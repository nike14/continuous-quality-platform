"""Signed webhooks through the ingestion API.

From Pact Broker, Argo Rollouts and production tools.
"""

import hashlib
import hmac
import json

import pytest
from conftest import SECRET

PACT = {
    "provider": "payments-api",
    "provider_version": "9f3c",
    "consumer": "checkout-web",
    "verification_url": "https://pact/verifications/77",
    "success": False,
}


def post_hook(client, body: bytes, secret=SECRET, tool="pact-broker"):
    # Signs the body the way each tool's webhook template does.
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(f"/v1/hooks/{tool}", content=body, headers={"X-CQP-Signature": sig})


def test_signed_pact_webhook_becomes_contract_event(client, published):
    r = post_hook(client, json.dumps(PACT).encode())

    assert r.status_code == 202
    _, key, event = published[0]
    # Pact's provider and provider version become service and commit.
    assert key == "payments-api:9f3c"
    assert event["check"] == "tests.contract"
    assert event["payload"] == {"status": "fail", "consumer": "checkout-web"}


def test_wrong_signature_is_rejected(client, published):
    r = post_hook(client, json.dumps(PACT).encode(), secret="guess")
    assert r.status_code == 401 and published == []


def test_unknown_tool_is_rejected(client):
    assert post_hook(client, b"{}", tool="mystery").status_code == 404


def test_malformed_payload_is_rejected(client, published):
    r = post_hook(client, json.dumps({"provider": "x"}).encode())
    assert r.status_code == 422 and published == []


def test_canary_result_is_recorded_against_the_image(client, published):
    body = {
        "service": "payments-api",
        "commit": "9f3c",
        "image": "sha256:ab12",
        "analysis_run": "payments-api-7f9-1",
        "phase": "Failed",
    }
    assert post_hook(client, json.dumps(body).encode(), tool="argo-rollouts").status_code == 202

    # The result is stored against the image that was being rolled out.
    event = published[0][2]
    assert event["check"] == "deploy.canary" and event["image"] == "sha256:ab12"
    assert event["payload"]["status"] == "fail"


def test_production_state_becomes_service_level_events(client, published):
    for body in (
        {"type": "slo", "service": "payments-api", "error_budget_remaining": 0, "at": "t1"},
        {
            "type": "incident",
            "service": "payments-api",
            "id": "INC-7",
            "severity": "sev1",
            "state": "open",
        },
        {"type": "freeze", "active": True, "at": "t2"},
    ):
        assert post_hook(client, json.dumps(body).encode(), tool="runtime").status_code == 202

    # A freeze is organisation-wide, so its service is "*".
    checks = [(e["service"], e["check"], e["payload"]["status"]) for _, _, e in published]
    assert checks == [
        ("payments-api", "runtime.slo", "fail"),
        ("payments-api", "runtime.incident", "fail"),
        ("*", "org.freeze", "fail"),
    ]


def test_runtime_events_keep_the_time_the_tool_observed_them(client, published):
    body = {
        "type": "slo",
        "service": "payments-api",
        "error_budget_remaining": 0.4,
        "at": "t3",
        "observed_at": "2026-10-07T09:59:00Z",
    }
    assert post_hook(client, json.dumps(body).encode(), tool="runtime").status_code == 202
    assert published[0][2]["observed_at"] == "2026-10-07T09:59:00+00:00"


@pytest.mark.parametrize("at", ["yesterday", "2026-10-07T09:59:00", 5])
def test_an_unreadable_or_zoneless_observed_at_is_rejected(client, published, at):
    body = {
        "type": "slo",
        "service": "payments-api",
        "error_budget_remaining": 0.4,
        "at": "t4",
        "observed_at": at,
    }
    assert post_hook(client, json.dumps(body).encode(), tool="runtime").status_code == 422
    assert published == []


def test_unknown_runtime_event_type_is_rejected(client, published):
    r = post_hook(client, json.dumps({"type": "weather"}).encode(), tool="runtime")
    assert r.status_code == 422 and published == []
