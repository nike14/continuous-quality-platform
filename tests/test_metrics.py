"""The metrics the alert rules depend on actually move."""

import json

import httpx
import pytest
from cqp.notify.gitlab import GitLab
from cqp.store.worker import process
from prometheus_client import REGISTRY

AUTH = {"Authorization": "Bearer good-token"}


def value(name, **labels):
    # 0 when the series doesn't exist yet.
    return REGISTRY.get_sample_value(name, labels) or 0


def upload(client, check="tests.unit", headers=AUTH):
    meta = {"service": "payments-api", "check": check, "format": "junit"}
    return client.post(
        "/v1/reports",
        data={"meta": json.dumps(meta)},
        files=[("files", ("junit.xml", b"<testsuite/>"))],
        headers=headers,
    )


def test_ingestion_counts_accepted_and_rejected_evidence(client):
    before_ok = value("cqp_ingest_events_total", source="ci", result="accepted")
    before_auth = value("cqp_ingest_events_total", source="ci", result="rejected_auth")

    # One good upload and one with a forged token.
    upload(client)
    upload(client, headers={"Authorization": "Bearer forged"})

    assert value("cqp_ingest_events_total", source="ci", result="accepted") == before_ok + 1
    assert value("cqp_ingest_events_total", source="ci", result="rejected_auth") == before_auth + 1


def test_metrics_endpoint_is_served(client):
    r = client.get("/metrics")
    assert r.status_code == 200 and "cqp_ingest_events_total" in r.text


# Just enough of a Kafka message and consumer for process().
class Msg:
    def __init__(self, event):
        self._value = json.dumps(event).encode()

    def value(self):
        return self._value

    def key(self):
        return b"k"


class Consumer:
    def commit(self, message, asynchronous):
        pass


def test_store_counts_parked_events(conn):
    before = value("cqp_store_events_total", result="dlq")
    # An unknown format can't be parsed, so the event must be parked in the DLQ.
    bad = {
        "event_id": "x",
        "service": "s",
        "commit": "c",
        "image": None,
        "check": "tests.unit",
        "format": "mystery",
        "run": "1",
        "producer": "ci",
        "payload": None,
        "raw_ref": None,
        "received_at": "2026-10-07T10:00:00+00:00",
    }
    process(Msg(bad), conn, Consumer(), lambda *a: None)
    assert value("cqp_store_events_total", result="dlq") == before + 1


def test_gitlab_outage_is_counted_as_a_notifier_error():
    def down(request):
        return httpx.Response(503)

    gitlab = GitLab(
        httpx.Client(base_url="https://gitlab/api/v4", transport=httpx.MockTransport(down))
    )
    before = value("cqp_notifications_total", target="gitlab", result="error")
    # A 5xx is raised so the worker retries the message, and it's counted as an error.
    with pytest.raises(httpx.HTTPStatusError):
        gitlab.retry_job("payments/payments-api", "1")
    assert value("cqp_notifications_total", target="gitlab", result="error") == before + 1


def test_alerting_series_exist_before_the_first_event():
    # Regression: without this, DeadLetterGrowing never fired for the first parked report.
    assert REGISTRY.get_sample_value("cqp_store_events_total", {"result": "dlq"}) is not None
    assert (
        REGISTRY.get_sample_value("cqp_notifications_total", {"target": "slack", "result": "error"})
        is not None
    )
