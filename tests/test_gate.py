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


def test_gives_up_after_max_wait_and_asks_for_validation():
    client, _ = cqp_answering(
        decision("pending", "REQUIRE_VALIDATION", waiting=[{"check": "perf.load"}])
    )
    assert run(client, max_wait=30) == 2


def test_unreachable_cqp_continues_before_production_but_not_in_it():
    client, _ = cqp_answering("down")
    assert run(client, "pr") == 0
    assert run(client, "prod-deploy") == 2


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
