"""The `cqp` command's entry point: argument wiring, defaults, and missing settings.

The report, gate and policy logic have their own tests; these check main() routes to them.
"""

import json

import httpx
import pytest
from cqp.gather import cli


@pytest.fixture(autouse=True)
def ci_job(tmp_path, monkeypatch):
    # A CI job's environment, in a fresh folder with one report.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "junit.xml").write_text("<testsuite/>")
    for name in ("CQP_URL", "CQP_TOKEN", "CI_PROJECT_NAME", "CI_MERGE_REQUEST_IID"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def cqp(monkeypatch):
    """A fake CQP behind every httpx.Client the command creates; returns the requests sent."""
    # httpx.Client is swapped for the whole test, so keep the real one to build the fake.
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/v1/reports":
            return httpx.Response(202, json={"event_id": "e-1"})
        if request.url.path == "/v1/decisions":
            return httpx.Response(
                200,
                json={
                    "decision_id": "d-1",
                    "status": "final",
                    "outcome": "BLOCKED",
                    "policy_version": "abc",
                    "findings": [],
                    "waiting": [],
                },
            )
        return httpx.Response(201, json={"kill_id": "k-1", "rule": "r", "expires_at": "t"})

    transport, real_client = httpx.MockTransport(handler), httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=transport))
    monkeypatch.setenv("CQP_URL", "https://cqp")
    monkeypatch.setenv("CQP_TOKEN", "DUMMY_token")
    return calls


def test_report_without_settings_warns_and_never_fails_the_job(capsys):
    assert (
        cli.main(["report", "--check", "tests.unit", "--format", "junit", "--files", "*.xml"]) == 0
    )
    assert "missing" in capsys.readouterr().err


@pytest.mark.parametrize("stage, code", [("pr", 0), ("prod-deploy", 2)])
def test_gate_without_settings_uses_the_fallback(stage, code):
    assert cli.main(["gate", "--service", "payments-api", "--stage", stage]) == code


def test_policy_without_settings_fails():
    assert cli.main(["policy", "check", "--service", "payments-api"]) == 1


def test_report_takes_the_service_from_the_ci_project_name(cqp, monkeypatch):
    monkeypatch.setenv("CI_PROJECT_NAME", "payments-api")
    assert (
        cli.main(["report", "--check", "tests.unit", "--format", "junit", "--files", "*.xml"]) == 0
    )
    assert b'"service": "payments-api"' in cqp[0].content


def test_gate_turns_the_answer_into_an_exit_code_and_sends_the_fix_ticket(cqp):
    code = cli.main(
        [
            "gate",
            "--service",
            "payments-api",
            "--stage",
            "prod-deploy",
            "--fix",
            "INC-7",
            "--max-wait",
            "0",
        ]
    )
    assert code == 1  # BLOCKED
    assert json.loads(cqp[0].content)["fix_ticket"] == "INC-7"


def test_kill_switch_needs_no_service(cqp):
    assert (
        cli.main(["kill-switch", "--rule", "baseline.tests.unit", "--reason", "bad release"]) == 0
    )
    assert cqp[0].url.path == "/v1/kill-switches"
