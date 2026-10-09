"""Notifier against a real Postgres, with GitLab and Slack faked."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cqp.notify import db
from cqp.notify.worker import decode, handle, post_to_slack, render, sweep
from cqp.store.db import save
from prometheus_client import REGISTRY

T0 = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
IMAGE = "sha256:ab12"


# Records what would be sent to GitLab instead of sending it.
class FakeGitLab:
    def __init__(self):
        self.comments, self.statuses, self.retried = [], [], []

    def upsert_mr_comment(self, project, mr, body):
        self.comments.append((project, mr, body))

    def set_status(self, project, sha, state, name, description):
        self.statuses.append((state, name))

    def retry_job(self, project, job_id):
        self.retried.append(job_id)


# A decision event as the Decision API publishes it; blocked by a failing unit test by default.
def decision(
    stage="pr",
    status="final",
    outcome="BLOCKED",
    findings=None,
    waiting=(),
    subject="9f3c",
    merge_request=7,
):
    return {
        "decision_id": "d-1",
        "service": "payments-api",
        "subject": subject,
        "stage": stage,
        "status": status,
        "outcome": outcome,
        "policy_version": "abc",
        "findings": findings
        if findings is not None
        else [
            {"rule": "baseline.tests.unit", "effect": "block", "message": "tests.unit failed: A.b"}
        ],
        "waiting": list(waiting),
        "created_at": T0.isoformat(),
        "context": {
            "project": "payments/payments-api",
            "job_id": 4412,
            "commit": "9f3c",
            "merge_request": merge_request,
            "pipeline_id": 99,
        },
    }


def test_comment_explains_the_decision():
    body = render(decision())
    assert body.startswith("<!-- cqp -->")
    assert "Blocked" in body and "tests.unit failed: A.b" in body and "d-1" in body


def test_blocked_pr_gets_a_comment_and_a_failed_status(conn):
    gitlab = FakeGitLab()
    posted: list[str] = []
    handle(decision(), gitlab, conn, posted.append)

    assert gitlab.comments[0][:2] == ("payments/payments-api", 7)
    assert gitlab.statuses == [("failed", "cqp/pr")]
    assert posted == [] and db.waiting(conn) == []  # no Slack for a PR


def test_production_problem_goes_to_slack(conn):
    posted: list[str] = []
    handle(decision("prod-deploy", merge_request=None), FakeGitLab(), conn, posted.append)
    assert "BLOCKED at prod-deploy" in posted[0] and "tests.unit failed" in posted[0]


def test_pending_decision_remembers_the_gate_job_and_final_one_forgets_it(conn):
    gitlab = FakeGitLab()
    deadline = (T0 + timedelta(hours=2)).isoformat()
    handle(
        decision(
            "integration",
            "pending",
            "REQUIRE_VALIDATION",
            [],
            [{"check": "perf.load", "deadline": deadline}],
            subject=IMAGE,
        ),
        gitlab,
        conn,
    )

    assert gitlab.statuses == [("pending", "cqp/integration")]
    # The gate job is remembered, so the sweep can retry it later.
    assert db.waiting(conn)[0][1] == "4412"

    # The final decision for the same change ends the wait.
    handle(decision("integration", outcome="PROCEED", findings=[], subject=IMAGE), gitlab, conn)
    assert db.waiting(conn) == []


# ---- Resuming gate jobs that are waiting ----


def remember_perf_wait(conn):
    db.remember(
        conn,
        "payments/payments-api",
        4412,
        "payments-api",
        IMAGE,
        "integration",
        ["perf.load"],
        T0 + timedelta(hours=2),
    )
    conn.commit()


def test_sweep_retries_the_gate_once_the_evidence_arrives(conn):
    remember_perf_wait(conn)
    gitlab = FakeGitLab()

    sweep(conn, gitlab, T0 + timedelta(minutes=5))
    assert gitlab.retried == []  # still waiting

    save(
        conn,
        {
            "event_id": "perf",
            "service": "payments-api",
            "commit": "9f3c",
            "image": IMAGE,
            "check": "perf.load",
            "kind": "measurement",
            "status": "pass",
            "run": "1",
            "producer": "ci",
            "payload": {},
            "raw_ref": None,
            "observed_at": T0.isoformat(),
        },
    )
    sweep(conn, gitlab, T0 + timedelta(minutes=6))
    assert gitlab.retried == ["4412"] and db.waiting(conn) == []


def test_sweep_retries_the_gate_when_the_deadline_passes(conn):
    remember_perf_wait(conn)
    gitlab = FakeGitLab()
    sweep(conn, gitlab, T0 + timedelta(hours=2))
    assert gitlab.retried == ["4412"]


# ---- Break-glass and waiver reminders ----


def test_break_glass_is_announced_at_any_stage(conn):
    posted: list[str] = []
    bg = {
        "rule": "baseline.break_glass",
        "effect": "warn",
        "message": "break-glass by sre: outage (INC-1)",
    }
    handle(
        decision("pr", outcome="PROCEED_WITH_WARNING", findings=[bg]),
        FakeGitLab(),
        conn,
        posted.append,
    )
    assert len(posted) == 1 and "🚨" in posted[0] and "INC-1" in posted[0]


def test_expiring_waiver_is_reminded_once(conn):
    from cqp.decide import db as decide_db
    from cqp.notify.worker import remind

    # One waiver expires tomorrow, the other in 20 days.
    decide_db.insert_waiver(
        conn,
        {
            "waiver_id": "w1",
            "service": "payments-api",
            "rule": "baseline.scan.sca",
            "reason": "r",
            "ticket": "SEC-1",
            "approver": "secops",
            "created_at": T0,
            "expires_at": T0 + timedelta(days=1),
        },
    )
    decide_db.insert_waiver(
        conn,
        {
            "waiver_id": "w2",
            "service": "payments-api",
            "rule": "baseline.tests.e2e",
            "reason": "r",
            "ticket": "PAY-2",
            "approver": "lead",
            "created_at": T0,
            "expires_at": T0 + timedelta(days=20),
        },
    )
    posted: list[str] = []
    remind(conn, posted.append, T0)
    remind(conn, posted.append, T0)  # the next sweep: already reminded, so nothing new
    assert len(posted) == 1 and "SEC-1" in posted[0] and "secops" in posted[0]


def test_slack_posts_are_counted_and_failures_raised(monkeypatch):
    sent = []

    def fake_post(url, json, timeout):
        sent.append(json)
        return httpx.Response(
            500 if json["text"] == "fail" else 200, request=httpx.Request("POST", url)
        )

    # Stand in for Slack's webhook endpoint.
    monkeypatch.setattr(httpx, "post", fake_post)

    def count(result):
        return REGISTRY.get_sample_value(
            "cqp_notifications_total", {"target": "slack", "result": result}
        )

    ok, error = count("ok"), count("error")

    post_to_slack("https://hooks.slack.example/DUMMY_hook", "deploy blocked")
    with pytest.raises(httpx.HTTPStatusError):  # raised, so the worker retries the message
        post_to_slack("https://hooks.slack.example/DUMMY_hook", "fail")

    assert sent[0] == {"text": "deploy blocked"}
    assert count("ok") == ok + 1 and count("error") == error + 1


@pytest.mark.parametrize(
    "value", [None, b"", b"not json", b"null", b"[1, 2]", b'{"no": "context"}']
)
def test_a_message_that_is_not_a_decision_is_skipped(value):
    # Returning None makes the worker commit past it instead of crashing on it again and again.
    assert decode(value) is None


def test_a_decision_message_is_decoded():
    assert decode(b'{"decision_id": "d-1", "context": {}}') == {"decision_id": "d-1", "context": {}}
