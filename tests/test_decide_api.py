"""Decision API end to end against real Postgres and OPA.

Kafka is faked: published events land in a list.
"""

from datetime import timedelta

import psycopg
import pytest
from conftest import CLAIMS, POLICY_DIR, T0, fake_verify
from cqp.decide.app import create_app, replay
from cqp.decide.policy import Policy
from cqp.store.db import save
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer good-token"}
IMAGE = "sha256:ab12"


# Puts a stored signal in place, as if it had come through Kafka and the store worker.
def record(conn, check, status="pass", payload=None, image=None):
    save(
        conn,
        {
            "event_id": f"{check}-{status}",
            "service": "payments-api",
            "commit": CLAIMS["sha"],
            "image": image,
            "check": check,
            "kind": "outcome",
            "status": status,
            "run": "1",
            "producer": "ci",
            "payload": payload or {},
            "raw_ref": None,
            "observed_at": "2026-10-07T09:00:00Z",
        },
    )
    # Committed, because the API reads through its own connection.
    conn.commit()


DEPLOY = {
    "Authorization": "Bearer deployer-token"
}  # production is asked about by the deploy system


def ask(api, stage, **body):
    headers = DEPLOY if stage == "prod-deploy" else AUTH
    r = api.post(
        "/v1/decisions", json={"service": "payments-api", "stage": stage, **body}, headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---- Outcomes ----


def test_failing_unit_tests_block_and_the_decision_can_be_replayed(setup, conn):
    api, connect, policy = setup
    record(conn, "tests.unit", "fail", {"failed": 1, "failing": ["PaymentRefundTest.partial"]})
    record(conn, "scan.secrets")

    d = ask(api, "pr")

    assert d["status"] == "final" and d["outcome"] == "BLOCKED"
    assert "PaymentRefundTest.partial" in d["findings"][0]["message"]
    assert api.get(f"/v1/decisions/{d['decision_id']}").json()["outcome"] == "BLOCKED"
    # Same input, same rules: a replay must give the same outcome.
    assert replay(connect, policy, d["decision_id"])


def test_all_expected_evidence_passing_proceeds(setup, conn):
    api, _, _ = setup
    record(conn, "tests.unit")
    record(conn, "scan.secrets")
    assert ask(api, "pr")["outcome"] == "PROCEED"


def test_missing_check_applies_its_on_missing_effect(setup, conn):
    api, _, _ = setup
    record(conn, "tests.unit")  # scan.secrets never reported; at PR that's a warning
    d = ask(api, "pr")
    assert d["outcome"] == "PROCEED_WITH_WARNING"
    assert d["findings"][0]["rule"] == "baseline.evidence_required"


# ---- Waiting for evidence ----


def test_waits_for_the_callers_own_uploads_then_decides(setup, conn, now):
    api, _, _ = setup
    record(conn, "tests.unit")
    record(conn, "scan.secrets")

    d = ask(api, "pr", event_ids=["still-in-kafka"])
    assert d["status"] == "pending" and d["waiting"] == [{"event_id": "still-in-kafka"}]

    now[0] = T0 + timedelta(minutes=2)  # past the in-flight grace period
    assert ask(api, "pr", event_ids=["still-in-kafka"])["status"] == "final"


def test_long_check_is_pending_until_its_deadline(setup, conn, now):
    api, _, _ = setup
    record(conn, "tests.integration", image=IMAGE)  # perf.load (2h deadline) hasn't reported

    d = ask(api, "integration", image=IMAGE)
    assert d["status"] == "pending" and d["outcome"] == "REQUIRE_VALIDATION"
    assert d["waiting"][0]["check"] == "perf.load"

    now[0] = T0 + timedelta(minutes=121)
    d = ask(api, "integration", image=IMAGE)
    assert d["status"] == "final" and d["outcome"] == "PROCEED_WITH_WARNING"


# ---- Publishing, outages and bad tokens ----


def test_decision_is_published_with_routing_from_the_token(setup, conn, published):
    api, _, _ = setup
    record(conn, "tests.unit")
    record(conn, "scan.secrets")

    d = ask(api, "pr", merge_request=7)

    topic, key, event = published[0]
    assert topic == "cqp.decisions" and key == f"payments-api:{CLAIMS['sha']}"
    assert event["decision_id"] == d["decision_id"]
    # Where to notify comes from the token, so a job can only notify its own project.
    assert event["context"] == {
        "project": CLAIMS["project_path"],
        "pipeline_id": None,
        "job_id": CLAIMS["job_id"],
        "commit": CLAIMS["sha"],
        "merge_request": 7,
    }


def test_kafka_outage_does_not_stop_the_decision(conn, pg_url, opa_url):
    # Its own app, so it can have a publisher that always fails.
    policy = Policy(POLICY_DIR, opa_url)
    policy.install()

    def broken_publish(*args):
        raise RuntimeError("Kafka down")

    api = TestClient(
        create_app(lambda: psycopg.connect(pg_url), policy, fake_verify, broken_publish)
    )
    r = api.post("/v1/decisions", json={"service": "payments-api", "stage": "pr"}, headers=AUTH)
    assert r.status_code == 200


def test_bad_token_is_rejected(setup):
    api, _, _ = setup
    r = api.post(
        "/v1/decisions",
        json={"service": "payments-api", "stage": "pr"},
        headers={"Authorization": "Bearer forged"},
    )
    assert r.status_code == 401


# ---- Production state ----


# A production signal (SLO, incident, freeze) as the runtime webhook would store it.
def runtime_event(conn, check, payload, at, service="payments-api"):
    save(
        conn,
        {
            "event_id": f"{check}-{at}",
            "service": service,
            "commit": "runtime",
            "image": None,
            "check": check,
            "kind": "health",
            "status": payload.get("status", "pass"),
            "run": at,
            "producer": "adapter:runtime",
            "payload": payload,
            "raw_ref": None,
            "observed_at": at,
        },
    )
    conn.commit()


@pytest.fixture
def ready_for_prod(conn):
    """Every check a tier-1 prod deploy expects, passing, for IMAGE.

    Plus fresh, healthy SLO data.
    """
    for check in [
        "build.provenance",
        "tests.unit",
        "tests.contract",
        "scan.sca",
        "scan.secrets",
        "tests.integration",
        "perf.load",
    ]:
        record(conn, check, image=IMAGE)
    # One minute old, so production health counts as fresh.
    runtime_event(
        conn,
        "runtime.slo",
        {"status": "pass", "error_budget_remaining": 0.4},
        (T0 - timedelta(minutes=1)).isoformat(),
    )


def test_healthy_production_proceeds_and_the_snapshot_is_recorded(setup, ready_for_prod):
    api, _, _ = setup
    d = ask(api, "prod-deploy", image=IMAGE)
    assert d["outcome"] == "PROCEED"
    assert d["input"]["runtime"]["health_fresh"] is True


def test_open_incident_needs_approval_but_a_fix_goes_out(setup, conn, ready_for_prod):
    api, _, _ = setup
    runtime_event(
        conn,
        "runtime.incident",
        {"status": "fail", "id": "INC-7", "severity": "sev1", "state": "open"},
        (T0 - timedelta(minutes=10)).isoformat(),
    )
    assert ask(api, "prod-deploy", image=IMAGE)["outcome"] == "REQUIRE_VALIDATION"
    # A fix still goes out during an incident; that's how incidents get resolved.
    assert ask(api, "prod-deploy", image=IMAGE, fix=True)["outcome"] == "PROCEED"


def test_failed_canary_in_the_last_day_warns(setup, conn, ready_for_prod):
    api, _, _ = setup
    # A canary of the previous image failed 3 hours ago.
    save(
        conn,
        {
            "event_id": "canary",
            "service": "payments-api",
            "commit": CLAIMS["sha"],
            "image": "sha256:previous",
            "check": "deploy.canary",
            "kind": "health",
            "status": "fail",
            "run": "r1",
            "producer": "adapter:argo-rollouts",
            "payload": {"status": "fail"},
            "raw_ref": None,
            "observed_at": (T0 - timedelta(hours=3)).isoformat(),
        },
    )
    conn.commit()
    assert ask(api, "prod-deploy", image=IMAGE)["outcome"] == "PROCEED_WITH_WARNING"


def test_change_freeze_blocks_every_service(setup, conn, ready_for_prod):
    api, _, _ = setup
    runtime_event(
        conn,
        "org.freeze",
        {"status": "fail", "active": True},
        (T0 - timedelta(minutes=30)).isoformat(),
        service="*",
    )
    assert ask(api, "prod-deploy", image=IMAGE)["outcome"] == "BLOCKED"


def test_stale_production_health_needs_approval_for_tier_1(setup, conn, now, ready_for_prod):
    api, _, _ = setup
    now[0] = T0 + timedelta(minutes=10)  # last SLO update is now 11 minutes old
    assert ask(api, "prod-deploy", image=IMAGE)["outcome"] == "REQUIRE_VALIDATION"


def test_decisions_and_findings_are_counted(setup, conn):
    from prometheus_client import REGISTRY

    api, _, _ = setup
    record(conn, "tests.unit", "fail", {"failed": 1, "failing": ["A.b"]})
    record(conn, "scan.secrets")
    labels = {"stage": "pr", "status": "final", "outcome": "BLOCKED"}
    # Metrics are shared by the whole test run, so compare before and after.
    before = REGISTRY.get_sample_value("cqp_decisions_total", labels) or 0

    ask(api, "pr")

    assert REGISTRY.get_sample_value("cqp_decisions_total", labels) == before + 1
    findings = REGISTRY.get_sample_value(
        "cqp_findings_total", {"rule": "baseline.tests.unit", "effect": "block"}
    )
    assert (findings or 0) >= 1
    assert (REGISTRY.get_sample_value("cqp_decision_seconds_count") or 0) >= 1


# ---- Who may ask ----


def test_a_repo_cannot_gate_on_a_service_it_does_not_own(setup):
    api, _, _ = setup
    r = api.post("/v1/decisions", json={"service": "checkout-web", "stage": "pr"}, headers=AUTH)
    assert r.status_code == 403


def test_only_the_deploy_system_asks_about_production(setup):
    api, _, _ = setup
    body = {"service": "payments-api", "stage": "prod-deploy", "image": IMAGE}
    assert (
        api.post("/v1/decisions", json=body, headers=AUTH).status_code == 403
    )  # the service itself
    assert (
        api.post(
            "/v1/decisions", json=body, headers={"Authorization": "Bearer deployer-branch-token"}
        ).status_code
        == 403
    )
    assert (
        api.post("/v1/decisions", json={**body, "image": None}, headers=DEPLOY).status_code == 422
    )


def test_deployer_sees_evidence_from_the_commit_that_built_the_image(setup, conn):
    api, _, _ = setup
    record(conn, "build.provenance", image=IMAGE)  # links IMAGE to the service's commit
    record(conn, "tests.unit", "fail", {"failed": 1, "failing": ["A.b"]})  # on the commit only
    d = ask(api, "prod-deploy", image=IMAGE)
    assert "tests.unit" in d["input"]["evidence"]


def test_a_token_without_a_commit_gets_a_clear_error(setup):
    api, _, _ = setup
    r = api.post(
        "/v1/decisions",
        json={"service": "payments-api", "stage": "pr"},
        headers={"Authorization": "Bearer no-sha-token"},
    )
    assert r.status_code == 422 and "no commit" in r.text


def test_replaying_an_unknown_decision_says_so(setup):
    _, connect, policy = setup
    with pytest.raises(LookupError, match="no decision"):
        replay(connect, policy, "nope")
