"""Metrics for operating CQP itself.

The alert rules in ops/prometheus/alerts.yml use these names.
"""

from fastapi import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# Gather
INGESTED = Counter(
    "cqp_ingest_events_total",
    "Evidence the ingestion API accepted or rejected",
    ["source", "result"],
)

# Store
STORED = Counter("cqp_store_events_total", "Raw events handled by the store worker", ["result"])
EVIDENCE_DELAY = Histogram(
    "cqp_evidence_delay_seconds",
    "Upload to stored signal (SLO: 99% < 60s)",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)

# Decide
DECISIONS = Counter("cqp_decisions_total", "Decisions made", ["stage", "status", "outcome"])
FINDINGS = Counter(
    "cqp_findings_total",
    "Rule findings; a jump after a release means a bad rule",
    ["rule", "effect"],
)
DECISION_SECONDS = Histogram(
    "cqp_decision_seconds",
    "Time to answer a decision request (SLO: p99 < 1s)",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
DECISION_ERRORS = Counter(
    "cqp_decision_errors_total", "Decision requests that failed with a server error"
)
PUBLISH_FAILURES = Counter(
    "cqp_decision_publish_failures_total", "Decision events not published to Kafka"
)
BREAK_GLASS = Counter(
    "cqp_break_glass_total", "Emergency overrides; each one is reviewed", ["stage"]
)
KILL_SWITCHES = Gauge("cqp_kill_switches_active", "Rules the platform team has switched off")

# Notify
NOTIFICATIONS = Counter("cqp_notifications_total", "Calls the notifier made", ["target", "result"])
WAITING = Gauge("cqp_waiting_gate_jobs", "Gate jobs waiting on evidence")
GATE_RETRIES = Counter("cqp_gate_retries_total", "Waiting gate jobs the notifier retried")


# Start alerting series at 0. A labelled counter otherwise first appears at 1, and increase()
# never sees that first event, so an alert like DeadLetterGrowing would stay silent.
for _result in ("stored", "dlq"):
    STORED.labels(_result)
for _stage in ("pr", "build", "integration", "prod-deploy"):
    BREAK_GLASS.labels(_stage)
for _target in ("gitlab", "slack"):
    for _result in ("ok", "skipped", "error"):
        NOTIFICATIONS.labels(_target, _result)


def metrics_response() -> Response:
    """The /metrics endpoint for the two APIs (the workers serve it with start_http_server)."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
