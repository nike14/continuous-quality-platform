"""Ingestion API with Kafka and file storage faked (the `client` fixture in conftest)."""

import json

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


def test_a_repo_cannot_report_for_a_service_it_does_not_own(client, published):
    r = upload(client, {"service": "checkout-web", "check": "tests.unit", "format": "junit"})
    assert r.status_code == 403 and "doesn't own checkout-web" in r.text
    assert published == []
