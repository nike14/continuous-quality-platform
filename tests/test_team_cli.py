"""`cqp policy` and `cqp waiver` against a fake CQP.

Unlike `cqp report`, these fail the job when refused.
"""

import json
from pathlib import Path

import httpx
from cqp.decide import team_cli

BACKTEST = {
    "decisions": 3,
    "changed": 1,
    "examples": [
        {"stage": "integration", "was": "PROCEED", "would_be": "BLOCKED", "decision_id": "d1"}
    ],
}


def mock(status, body):
    # Fake CQP that always answers with this status and body.
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def policy_file(tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text("thresholds:\n  perf.load: {max: 500, effect: block}\n")
    return str(f)


def test_check_is_a_dry_run_and_prints_the_backtest(tmp_path, capsys):
    client, calls = mock(200, {"version": "abc", "backtest": BACKTEST})
    assert (
        team_cli.policy("check", "payments-api", policy_file(tmp_path), "https://cqp", "t", client)
        == 0
    )
    # check never publishes: it only validates and reports the backtest.
    assert calls[0].url.params["dry_run"] == "true"
    out = capsys.readouterr().out
    assert "1 of the last 3" in out and "PROCEED -> BLOCKED" in out


def test_invalid_policy_fails_the_job_with_the_reasons(tmp_path, capsys):
    client, _ = mock(422, {"detail": {"errors": ["scan.sca at build can't be looser"]}})
    assert (
        team_cli.policy(
            "publish", "payments-api", policy_file(tmp_path), "https://cqp", "t", client
        )
        == 1
    )
    assert "can't be looser" in capsys.readouterr().err


def test_refused_waiver_fails(capsys):
    client, _ = mock(403, {"detail": {"errors": ["only approvers can create waivers"]}})
    assert (
        team_cli.waiver(
            "payments-api", "baseline.tests.e2e", "flaky", "PAY-1", 7, "https://cqp", "t", client
        )
        == 1
    )
    assert "only approvers" in capsys.readouterr().err


DECISION = {
    "decision_id": "d-9",
    "service": "payments-api",
    "stage": "integration",
    "status": "final",
    "outcome": "REQUIRE_VALIDATION",
    "policy_version": "abc",
    "input": {"team_policy_version": "t1"},
    "findings": [
        {
            "rule": "team.perf.load.threshold",
            "effect": "validate",
            "message": "perf.load is 200 ms",
        },
        {
            "rule": "baseline.passed_on_rerun",
            "effect": "warn",
            "message": "tests.unit passed only on a rerun",
        },
        {"rule": "baseline.tests.e2e", "effect": "warn", "message": "tests.e2e failed: x (waived)"},
    ],
    "waiting": [],
}


def test_explain_says_what_to_do_about_each_finding(capsys):
    client, calls = mock(200, DECISION)
    assert team_cli.explain("d-9", "https://cqp", "t", client) == 0
    assert calls[0].headers["Authorization"] == "Bearer t"  # the record needs a token
    out = capsys.readouterr().out
    assert "REQUIRE_VALIDATION (final)" in out and "team policy t1" in out
    assert "cqp approve --decision d-9" in out and "quarantine it" in out
    assert out.count("→") == 2  # nothing to do about a waived warning


def test_hints_by_kind_of_finding():
    def hint(rule, effect):
        return team_cli.hint({"rule": rule, "effect": effect, "message": ""}, "d") or ""

    assert "waiver" in hint("baseline.scan.sca", "block")
    assert "flaky" in hint("baseline.tests.unit", "block")
    assert "didn't report" in hint("baseline.evidence_required", "block")
    assert "break-glass" in hint("baseline.change_freeze", "block")
    assert hint("baseline.static.quality", "warn") == ""
    # Approval is refused while something blocks, so don't suggest it then.
    blocked = team_cli.hint({"rule": "team.x", "effect": "validate", "message": ""}, "d", "BLOCKED")
    assert "blocks first" in (blocked or "")


def test_preview_reads_local_reports_and_exits_like_the_gate(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("junit.xml").write_text(
        '<testsuite><testcase classname="A" name="b"><failure/></testcase></testsuite>'
    )
    answer = {
        "outcome": "BLOCKED",
        "findings": [{"rule": "baseline.tests.unit", "effect": "block", "message": "A.b"}],
        "not_checked": ["scan.secrets"],
        "policy_version": "abc",
    }
    client, calls = mock(200, answer)
    assert (
        team_cli.preview("payments-api", "pr", ["tests.unit:junit:*.xml"], "https://cqp", client)
        == 1
    )
    sent = json.loads(calls[0].content)
    assert sent["evidence"]["tests.unit"]["status"] == "fail"
    assert sent["evidence"]["tests.unit"]["payload"]["failing"] == ["A.b"]
    assert "not checked locally" in capsys.readouterr().out


def test_preview_refuses_a_malformed_report_flag(capsys):
    client, calls = mock(200, {})
    assert team_cli.preview("payments-api", "pr", ["tests.unit"], "https://cqp", client) == 1
    assert not calls


def test_baseline_backtest_prints_what_would_change(tmp_path, capsys):
    rego = tmp_path / "baseline.rego"
    rego.write_text("package cqp.baseline\n")
    bt = {
        "candidate": "cand1",
        "current": "abc",
        "decisions": 9,
        "changed": 1,
        "examples": [
            {
                "service": "ledger",
                "stage": "pr",
                "was": "BLOCKED",
                "would_be": "PROCEED",
                "decision_id": "d1",
            }
        ],
    }
    client, calls = mock(200, bt)
    assert team_cli.baseline_backtest(str(rego), "https://cqp", "t", client) == 0
    assert json.loads(calls[0].content) == {"rego": "package cqp.baseline\n"}
    assert "1 of the last 9" in capsys.readouterr().out
