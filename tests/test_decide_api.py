"""Decision API end to end against real Postgres and OPA.

Kafka is faked: published events land in a list.
"""

from datetime import timedelta

import psycopg
import pytest
from conftest import CLAIMS, POLICY_DIR, T0, fake_verify
from cqp.decide import db as decide_db
from cqp.decide.app import create_app, replay
from cqp.decide.policy import Policy
from cqp.store.db import save
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer good-token"}
IMAGE = "sha256:ab12"


# Puts a stored signal in place, as if it had come through Kafka and the store worker.
def record(conn, check, status="pass", payload=None, image=None, at="2026-10-07T09:00:00Z"):
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
            "observed_at": at,
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
    path = f"/v1/decisions/{d['decision_id']}"
    assert (
        api.get(path, headers={"Authorization": "Bearer good-token"}).json()["outcome"] == "BLOCKED"
    )
    # The record carries the full evidence, so it needs a token.
    assert api.get(path).status_code == 401
    assert api.get(path, headers={"Authorization": "Bearer forged"}).status_code == 401
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
    fixed = ask(api, "prod-deploy", image=IMAGE, fix_ticket="INC-7")
    assert fixed["outcome"] == "PROCEED_WITH_WARNING"
    assert "released as a fix under INC-7" in fixed["findings"][0]["message"]


def test_resolving_one_incident_does_not_hide_another_still_open(setup, conn, ready_for_prod):
    api, _, _ = setup

    def incident(id_, state, minutes_ago, severity="sev2"):
        payload = {
            "status": "fail" if state == "open" else "pass",
            "id": id_,
            "severity": severity,
            "state": state,
        }
        runtime_event(
            conn, "runtime.incident", payload, (T0 - timedelta(minutes=minutes_ago)).isoformat()
        )

    incident("INC-A", "open", 30, severity="sev1")
    incident("INC-B", "open", 20)
    incident("INC-B", "resolved", 10)  # the newest incident event, but about B only
    d = ask(api, "prod-deploy", image=IMAGE)
    assert d["outcome"] == "REQUIRE_VALIDATION" and d["input"]["runtime"]["incident"] == "sev1"

    incident("INC-A", "resolved", 5)
    assert ask(api, "prod-deploy", image=IMAGE)["outcome"] == "PROCEED"


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


def test_repeat_polls_from_one_job_reuse_the_pending_decision(setup, conn, published):
    # Regression: each 5s poll stored a new decision and re-posted the MR comment and status.
    api, _, _ = setup
    record(conn, "tests.integration")  # perf.load (2h deadline) still running
    ids = {ask(api, "integration")["decision_id"] for _ in range(3)}
    assert len(ids) == 1 and len(published) == 1
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1


def test_a_retried_job_gets_its_own_pending_decision(setup, conn, published):
    # The notifier needs the new job's ID to resume it, so a different job must be published.
    api, _, _ = setup
    record(conn, "tests.integration")
    first = ask(api, "integration")
    r = api.post(
        "/v1/decisions",
        json={"service": "payments-api", "stage": "integration"},
        headers={"Authorization": "Bearer retried-job-token"},
    )
    assert r.json()["decision_id"] != first["decision_id"] and len(published) == 2


def test_a_poll_after_the_evidence_lands_is_a_new_final_decision(setup, conn, now):
    api, _, _ = setup
    record(conn, "tests.integration")
    pending = ask(api, "integration")
    record(conn, "perf.load", payload={"metric": "p95_ms", "value": 200, "unit": "ms"})
    final = ask(api, "integration")
    assert final["status"] == "final" and final["decision_id"] != pending["decision_id"]


def stored_decision(conn, version, outcome_):
    """A PR decision on a failing unit test, as an older Decision API would have stored it."""
    decide_db.insert(
        conn,
        {
            "decision_id": f"old-{version}",
            "service": "payments-api",
            "subject": CLAIMS["sha"],
            "stage": "pr",
            "status": "final",
            "outcome": outcome_,
            "findings": [],
            "waiting": [],
            "input": {
                "stage": "pr",
                "evidence": {
                    "tests.unit": {
                        "kind": "outcome",
                        "status": "fail",
                        "payload": {"failing": ["A.b"]},
                    }
                },
                "missing": [],
            },
            "policy_version": version,
            "created_at": T0,
        },
    )
    conn.commit()


def test_an_old_decision_replays_on_the_rules_it_was_made_with(setup, conn):
    # Regression: replay refused anything made under a previous baseline.
    _, connect, policy = setup
    # The old baseline only warned on failing unit tests at PR; today's blocks.
    old_rego = policy.rego.replace('"tests.unit": {"pr": "block"', '"tests.unit": {"pr": "warn"', 1)
    assert old_rego != policy.rego
    decide_db.save_policy_version(conn, "oldbaseline1", old_rego)
    stored_decision(conn, "oldbaseline1", "PROCEED_WITH_WARNING")
    assert replay(connect, policy, "old-oldbaseline1")


def test_replaying_a_decision_whose_rules_were_never_stored_says_so(setup, conn):
    _, connect, policy = setup
    stored_decision(conn, "neverstored1", "BLOCKED")
    with pytest.raises(ValueError, match="never stored"):
        replay(connect, policy, "old-neverstored1")


def test_a_security_scan_older_than_its_max_age_counts_as_missing(setup, conn, ready_for_prod):
    # Regression: a month-old scan used to count at the production gate.
    api, _, _ = setup
    # Swap the fresh scan for one from 30 days ago (same event ID, so delete first).
    conn.execute("DELETE FROM signals WHERE check_name = 'scan.sca'")
    record(conn, "scan.sca", image=IMAGE, at=(T0 - timedelta(days=30)).isoformat())
    d = ask(api, "prod-deploy", image=IMAGE)
    assert d["outcome"] == "BLOCKED"
    assert any("scan.sca result is too old" in f["message"] for f in d["findings"])


def test_a_scan_inside_its_max_age_still_counts(setup, conn, ready_for_prod):
    api, _, _ = setup
    assert ask(api, "prod-deploy", image=IMAGE)["outcome"] == "PROCEED"


def test_a_fix_without_a_ticket_is_refused(setup):
    api, _, _ = setup
    body = {"service": "payments-api", "stage": "prod-deploy", "image": IMAGE, "fix_ticket": ""}
    assert api.post("/v1/decisions", json=body, headers=DEPLOY).status_code == 422


def test_each_decision_records_the_services_adoption_mode(setup, conn):
    api, _, _ = setup
    assert ask(api, "pr")["mode"] == "enforce"
    r = api.post(
        "/v1/decisions",
        json={"service": "checkout-web", "stage": "pr"},
        headers={"Authorization": "Bearer other-repo-token"},
    )
    assert r.json()["mode"] == "advisory"
    assert (
        conn.execute("SELECT mode FROM decisions WHERE service = 'checkout-web'").fetchone()[0]
        == "advisory"
    )
