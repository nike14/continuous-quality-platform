"""Ingestion API with Kafka and file storage faked (the `client` fixture in conftest)."""

import json

import pytest
from conftest import CLAIMS

AUTH = {"Authorization": "Bearer good-token"}
# The content doesn't matter here; parsing happens later, in the store worker.
JUNIT = ("files", ("junit.xml", b"<testsuite tests='500' failures='20'/>"))


def upload(client, meta, headers=AUTH):
    return client.post(
        "/v1/reports", data={"meta": json.dumps(meta)}, files=[JUNIT], headers=headers
    )


def test_report_is_stored_and_published(client, published, stored):
    r = upload(
        client,
        {
            "service": "payments-api",
            "check": "tests.unit",
            "format": "junit",
            "image": "sha256:ab12",
        },
    )

    assert r.status_code == 202
    topic, key, event = published[0]
    assert topic == "cqp.signals.raw"
    # Keyed by service and commit, so one change's events stay in order.
    assert key == f"payments-api:{CLAIMS['sha']}"
    assert event["check"] == "tests.unit" and event["image"] == "sha256:ab12"
    assert event["raw_ref"] == f"mem://{event['event_id']}"
    assert stored[event["event_id"]][0][0] == "junit.xml"


def test_a_report_is_dated_when_its_job_started_not_when_it_arrived(client, published):
    meta = {"service": "payments-api", "check": "tests.unit", "format": "junit"}
    upload(client, meta, headers={"Authorization": "Bearer issued-token"})
    event = published[0][2]
    assert event["observed_at"] == "2026-10-07T10:00:00+00:00"
    assert event["received_at"] > event["observed_at"]


def test_identity_comes_from_token_not_payload(client, published):
    upload(
        client,
        {
            "service": "payments-api",
            "check": "tests.unit",
            "format": "junit",
            "commit": "forged",
            "repo": "someone/else",
        },
    )  # fields a job might try to slip in

    # The slipped-in fields are ignored; identity comes from the token.
    event = published[0][2]
    assert event["commit"] == CLAIMS["sha"]
    assert event["repo"] == CLAIMS["project_path"]
    assert event["run"] == str(CLAIMS["job_id"])


def test_same_upload_twice_has_same_event_id(client):
    # Same job, same report: a retried upload must not count twice.
    meta = {"service": "payments-api", "check": "tests.unit", "format": "junit"}
    assert upload(client, meta).json() == upload(client, meta).json()


def test_bad_token_is_rejected(client, published):
    r = upload(
        client,
        {"service": "s", "check": "tests.unit", "format": "junit"},
        headers={"Authorization": "Bearer forged"},
    )
    assert r.status_code == 401 and published == []


def test_unknown_check_is_rejected(client, published):
    r = upload(client, {"service": "s", "check": "tests.vibes", "format": "junit"})
    assert r.status_code == 422 and published == []


@pytest.mark.parametrize(
    "check, fmt",
    [("tests.contract", "pact"), ("tests.unit", "json")],  # no parser for either from CI
)
def test_a_format_the_store_cannot_read_is_refused_up_front(client, published, check, fmt):
    r = upload(client, {"service": "payments-api", "check": check, "format": fmt})
    assert r.status_code == 422 and published == []


def test_a_repo_cannot_report_for_a_service_it_does_not_own(client, published):
    r = upload(client, {"service": "checkout-web", "check": "tests.unit", "format": "junit"})
    assert r.status_code == 403 and "doesn't own checkout-web" in r.text
    assert published == []


def test_two_different_reports_from_one_job_are_both_kept(client):
    # Regression: the second report for the same check in the same job got the first's ID
    # and was dropped as a duplicate.
    meta = {"service": "payments-api", "check": "tests.unit", "format": "junit"}
    first = client.post(
        "/v1/reports",
        data={"meta": json.dumps(meta)},
        headers=AUTH,
        files=[("files", ("module-a.xml", b"<testsuite/>"))],
    ).json()
    second = client.post(
        "/v1/reports",
        data={"meta": json.dumps(meta)},
        headers=AUTH,
        files=[("files", ("module-b.xml", b"<testsuite/>"))],
    ).json()
    assert first["event_id"] != second["event_id"]


def test_an_upload_over_the_size_limit_is_refused(published):
    # Its own app with a 10-byte limit; the limit counts all files in the upload together.
    from conftest import fake_verify
    from cqp.gather.app import create_app
    from fastapi.testclient import TestClient

    app = create_app(
        fake_verify,
        lambda *a: published.append(a),
        lambda *a: "mem://x",
        {},
        owns=lambda project, service: True,
        max_upload_bytes=10,
    )
    meta = {"service": "payments-api", "check": "tests.unit", "format": "junit"}
    files = [("files", ("a.xml", b"123456")), ("files", ("b.xml", b"789012"))]
    r = TestClient(app).post(
        "/v1/reports", data={"meta": json.dumps(meta)}, files=files, headers=AUTH
    )
    assert r.status_code == 413 and published == []
