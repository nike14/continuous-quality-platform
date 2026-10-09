"""Consumes cqp.decisions: MR comment, commit status, Slack, and resuming waiting gate jobs."""

import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
from prometheus_client import start_http_server

from cqp.decide import db as decide_db
from cqp.decide.app import DECISIONS_TOPIC, IN_FLIGHT_GRACE
from cqp.metrics import GATE_RETRIES, NOTIFICATIONS, WAITING
from cqp.notify import db
from cqp.notify.gitlab import MARKER, GitLab, LogOnlyGitLab
from cqp.store.db import latest

SWEEP_SECONDS = 10
REMIND_BEFORE = timedelta(days=2)
log = logging.getLogger(__name__)
COMMIT_STATE = {
    "PROCEED": "success",
    "PROCEED_WITH_WARNING": "success",
    "REQUIRE_VALIDATION": "pending",
    "BLOCKED": "failed",
}
ICON = {"PROCEED": "✅", "PROCEED_WITH_WARNING": "⚠️", "REQUIRE_VALIDATION": "⏸️", "BLOCKED": "⛔"}


def render(d: dict) -> str:
    """The one MR comment: what was decided, why, and what we're still waiting for."""
    title = (
        "Waiting for evidence"
        if d["status"] == "pending"
        else d["outcome"].replace("_", " ").title()
    )
    # The hidden marker goes first, so the next decision updates this comment instead of adding one.
    lines = [MARKER, f"**{ICON[d['outcome']]} Quality: {title}** at `{d['stage']}`", ""]
    lines += [f"- **{f['effect']}** · {f['message']} (`{f['rule']}`)" for f in d["findings"]]
    lines += [f"- waiting for `{w.get('check') or w.get('event_id')}`" for w in d["waiting"]]
    if not d["findings"] and not d["waiting"]:
        lines.append("- all expected evidence is in and no rule objected")
    lines += ["", f"<sub>decision `{d['decision_id']}` · policy `{d['policy_version']}`</sub>"]
    return "\n".join(lines)


def handle(d: dict, gitlab: GitLab, conn, slack=None) -> None:
    """Acts on one decision: MR comment, commit status, waiting-job bookkeeping, Slack."""
    ctx = d["context"]
    if ctx.get("merge_request"):
        gitlab.upsert_mr_comment(ctx["project"], ctx["merge_request"], render(d))
    if ctx.get("commit"):
        # One status per stage (cqp/pr, cqp/build...), so GitLab shows each gate separately.
        state = "pending" if d["status"] == "pending" else COMMIT_STATE[d["outcome"]]
        gitlab.set_status(ctx["project"], ctx["commit"], state, f"cqp/{d['stage']}", d["outcome"])

    with conn.transaction():
        if d["status"] == "pending" and ctx.get("job_id"):
            # Remember the gate job so the sweep can retry it when the evidence is in.
            checks = [w["check"] for w in d["waiting"] if "check" in w]
            # Retry at the latest deadline;
            # if we're only waiting on in-flight uploads, after the grace.
            deadline = max(
                (datetime.fromisoformat(w["deadline"]) for w in d["waiting"] if "deadline" in w),
                default=datetime.fromisoformat(d["created_at"]) + IN_FLIGHT_GRACE,
            )
            db.remember(
                conn,
                ctx["project"],
                ctx["job_id"],
                d["service"],
                d["subject"],
                d["stage"],
                checks,
                deadline,
            )
        elif d["status"] == "final":
            db.forget(conn, d["service"], d["subject"], d["stage"])

    # Break-glass is always announced, at any stage, so it gets reviewed.
    glass = next((f for f in d["findings"] if f["rule"] == "baseline.break_glass"), None)
    if slack and glass:
        slack(f"🚨 {d['service']} {glass['message']} at {d['stage']} (decision {d['decision_id']})")
    # Otherwise Slack only where a person must act fast:
    # production decisions that aren't a clean proceed.
    elif (
        slack
        and d["stage"] == "prod-deploy"
        and d["status"] == "final"
        and d["outcome"] != "PROCEED"
    ):
        reason = d["findings"][0]["message"] if d["findings"] else d["outcome"]
        slack(
            f"{ICON[d['outcome']]} {d['service']} {d['outcome']} at prod-deploy: {reason} "
            f"(decision {d['decision_id']})"
        )


def sweep(conn, gitlab: GitLab, now: datetime) -> None:
    """Retries gate jobs whose missing checks have arrived or whose deadline has passed."""
    still_waiting = 0
    for project, job_id, service, subject, _stage, checks, deadline in db.waiting(conn):
        # subject is an image or a commit; passing it as both covers either case.
        arrived = latest(conn, service, image=subject, commit=subject)
        if now >= deadline or all(c in arrived for c in checks):
            gitlab.retry_job(project, job_id)  # CI re-runs its own gate, which asks again
            db.done(conn, project, job_id)
            GATE_RETRIES.inc()
        else:
            still_waiting += 1
    conn.commit()
    WAITING.set(still_waiting)


def remind(conn, notify, now: datetime) -> None:
    """One reminder per waiver, two days before it expires, so nobody is surprised by a block."""
    for w in decide_db.expiring_waivers(conn, now, REMIND_BEFORE):
        # Name the approver: they're the one who can renew it.
        notify(
            f"⏳ waiver {w['ticket']} on {w['service']} ({w['rule']}) expires "
            f"{w['expires_at']:%Y-%m-%d %H:%M} UTC; fix it or ask {w['approver']} to renew"
        )
        # Mark it, so the next sweep (every 10s) doesn't post it again.
        decide_db.reminded(conn, w["waiver_id"], now)
    conn.commit()


def post_to_slack(webhook: str, text: str) -> None:
    """Posts one message to the Slack webhook."""
    try:
        # raise_for_status turns a Slack error into an exception we count and re-raise.
        httpx.post(webhook, json={"text": text}, timeout=5).raise_for_status()
    except httpx.HTTPError:
        NOTIFICATIONS.labels("slack", "error").inc()
        raise
    NOTIFICATIONS.labels("slack", "ok").inc()


def main() -> None:
    """Reads decisions from Kafka and runs the sweep and reminders every few seconds."""
    from confluent_kafka import Consumer

    consumer = Consumer(
        {
            "bootstrap.servers": os.environ.get("CQP_KAFKA", "localhost:9092"),
            "group.id": "notifier",
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    api = os.environ.get("CQP_GITLAB_API")
    gitlab = (
        GitLab(
            httpx.Client(
                base_url=api, timeout=10, headers={"PRIVATE-TOKEN": os.environ["CQP_GITLAB_TOKEN"]}
            )
        )
        if api
        else LogOnlyGitLab()
    )
    webhook = os.environ.get("CQP_SLACK_WEBHOOK")
    slack = (lambda text: post_to_slack(webhook, text)) if webhook else None

    start_http_server(9100)  # /metrics for Prometheus
    with psycopg.connect(os.environ["CQP_DATABASE_URL"]) as conn:
        db.migrate(conn)
        decide_db.migrate(conn)  # the waivers table, for reminders
        consumer.subscribe([DECISIONS_TOPIC])
        last_sweep = 0.0
        while True:
            msg = consumer.poll(1.0)
            if msg is not None:
                if msg.error():
                    raise RuntimeError(msg.error())
                handle(json.loads(msg.value()), gitlab, conn, slack)
                consumer.commit(message=msg, asynchronous=False)  # after the side effects
            if time.monotonic() - last_sweep >= SWEEP_SECONDS:
                sweep(conn, gitlab, datetime.now(UTC))
                remind(conn, slack or log.info, datetime.now(UTC))
                last_sweep = time.monotonic()
