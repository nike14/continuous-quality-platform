"""`cqp gate` against a fake CQP.

Exit codes, polling, and what happens when CQP is down or says no.
"""

from pathlib import Path

import httpx
import pytest
from cqp.decide.gate import gate


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    # Two earlier jobs left their upload IDs behind, the way CI artifacts would.
    monkeypatch.chdir(tmp_path)
    Path(".cqp/events").mkdir(parents=True)
    Path(".cqp/events/101.txt").write_text("e-1\n")
    Path(".cqp/events/102.txt").write_text("e-2\n")


def decision(status="final", outcome="PROCEED", findings=(), waiting=()):
    return {
        "decision_id": "d-1",
        "status": status,
        "outcome": outcome,
        "policy_version": "abc",
        "findings": list(findings),
        "waiting": list(waiting),
    }


def cqp_answering(*answers):
    """Fake CQP giving these answers in order (the last one repeats). "down" means unreachable."""
    calls = []

    def handler(request):
        calls.append(request)
        # Past the end of the list, keep giving the last answer.
        answer = answers[min(len(calls), len(answers)) - 1]
        if answer == "down":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json=answer)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def run(client, stage="pr", max_wait=120):
    ticks = iter(range(0, 10_000, 10))  # every clock read moves time on 10s
    return gate(
        "payments-api",
        stage,
        "https://cqp",
        "tok",
        client,
        max_wait,
        sleep=lambda s: None,
        clock=lambda: next(ticks),
    )


def test_sends_every_jobs_event_ids_and_the_merge_request(monkeypatch):
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "7")
    client, calls = cqp_answering(decision())
    assert run(client) == 0
    body = calls[0].content.replace(
        b" ", b""
    )  # compare without spaces; JSON layout isn't the point
    assert b'"event_ids":["e-1","e-2"]' in body and b'"merge_request":7' in body


@pytest.mark.parametrize(
    "outcome,code",
    [("PROCEED", 0), ("PROCEED_WITH_WARNING", 0), ("REQUIRE_VALIDATION", 2), ("BLOCKED", 1)],
)
def test_outcome_becomes_exit_code(outcome, code):
    client, _ = cqp_answering(decision(outcome=outcome))
    assert run(client) == code


def test_polls_while_pending_then_uses_final_answer():
    client, calls = cqp_answering(
        decision("pending", "REQUIRE_VALIDATION"), decision(outcome="BLOCKED")
    )
    assert run(client) == 1 and len(calls) == 2


def test_gives_up_after_max_wait_with_its_own_exit_code(capsys):
    # Still waiting isn't "a person must approve": 3, not 2, and the log says it will resume.
    client, _ = cqp_answering(
        decision("pending", "REQUIRE_VALIDATION", waiting=[{"check": "perf.load"}])
    )
    assert run(client, max_wait=30) == 3
    assert "re-runs this job" in capsys.readouterr().err


def test_a_final_answer_that_isnt_proceed_points_at_explain(capsys):
    client, _ = cqp_answering(decision(outcome="REQUIRE_VALIDATION"))
    assert run(client) == 2
    assert "cqp explain d-1" in capsys.readouterr().err


def test_unreachable_cqp_continues_before_production_but_not_in_it():
    client, _ = cqp_answering("down")
    assert run(client, "pr") == 0
    assert run(client, "prod-deploy") == 2


def test_unreachable_cqp_still_stops_on_this_pipelines_own_known_failures(capsys):
    # cqp report recorded these. A failed unit test run is known-bad, CQP or not.
    Path(".cqp/results").mkdir()
    Path(".cqp/results/101-tests.unit.json").write_text('{"check": "tests.unit", "status": "fail"}')
    Path(".cqp/results/102-tests.e2e.json").write_text('{"check": "tests.e2e", "status": "fail"}')
    client, _ = cqp_answering("down")
    assert run(client, "pr") == 1
    assert "tests.unit failed" in capsys.readouterr().err


def test_unreachable_cqp_ignores_failures_the_baseline_doesnt_always_block():
    # A failing e2e run only warns at some stages, so without CQP it doesn't stop anything.
    Path(".cqp/results").mkdir()
    Path(".cqp/results/102-tests.e2e.json").write_text('{"check": "tests.e2e", "status": "fail"}')
    Path(".cqp/results/103-tests.unit.json").write_text("{not json")
    client, _ = cqp_answering("down")
    assert run(client, "pr") == 0


def test_a_refused_request_fails_the_job_instead_of_falling_back(capsys):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            403, json={"detail": "payments/payments-api doesn't own checkout-web"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert run(client, "pr") == 1 and len(calls) == 1  # no retries, no "continuing"
    assert "doesn't own checkout-web" in capsys.readouterr().err


def test_a_fix_sends_its_ticket():
    client, calls = cqp_answering(decision())
    gate(
        "payments-api",
        "prod-deploy",
        "https://cqp",
        "tok",
        client,
        sleep=lambda s: None,
        fix_ticket="INC-7",
    )
    assert b'"fix_ticket":"INC-7"' in calls[0].content.replace(b" ", b"")


# ---- Adoption modes ----


def test_advisory_mode_reports_a_block_but_never_stops_the_pipeline(capsys):
    client, _ = cqp_answering({**decision(outcome="BLOCKED"), "mode": "advisory"})
    assert run(client) == 0
    assert "advisory mode: this would be BLOCKED" in capsys.readouterr().err


def test_outside_enforce_mode_the_gate_never_waits():
    # A 2-hour check must not hold a pipeline that the answer can't stop anyway.
    pending = {
        **decision("pending", "REQUIRE_VALIDATION", waiting=[{"check": "perf.load"}]),
        "mode": "shadow",
    }
    client, calls = cqp_answering(pending)
    assert run(client) == 0 and len(calls) == 1
