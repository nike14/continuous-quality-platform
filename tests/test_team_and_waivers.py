"""Team policy files and waivers through the Decision API (real Postgres and OPA)."""

from datetime import timedelta

from conftest import T0
from cqp.decide.app import replay
from test_decide_api import AUTH, ask, record

SLOW_PAYLOAD = {"metric": "p95_ms", "value": 900, "unit": "ms"}
THRESHOLD = {"thresholds": {"perf.load": {"max": 500, "effect": "block"}}}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def put(api, body, token="good-token", service="payments-api", dry_run=False):
    return api.put(
        f"/v1/team-policies/{service}",
        params={"dry_run": dry_run},
        json=body,
        headers=bearer(token),
    )


def waive(api, rule, token="approver-token", days=7):
    return api.post(
        "/v1/waivers",
        json={
            "service": "payments-api",
            "rule": rule,
            "days": days,
            "reason": "fix in progress",
            "ticket": "PAY-7",
        },
        headers=bearer(token),
    )


def slow_integration(conn):
    record(conn, "tests.integration")
    record(conn, "perf.load", payload=SLOW_PAYLOAD)


# ---- Publishing ----


def test_only_the_owning_repo_can_publish(setup):
    api, _, _ = setup
    assert put(api, THRESHOLD, token="other-repo-token").status_code == 403


def test_publishing_needs_a_protected_branch_but_a_dry_run_does_not(setup):
    api, _, _ = setup
    assert put(api, THRESHOLD, token="branch-token").status_code == 403
    assert put(api, THRESHOLD, token="branch-token", dry_run=True).status_code == 200
    assert api.get("/v1/team-policies/payments-api").json()["version"] is None


def test_unknown_keys_and_looser_than_baseline_are_rejected(setup):
    api, _, _ = setup
    # A misspelt key is rejected, not silently ignored.
    r = put(api, {"tresholds": {}})
    assert r.status_code == 422 and "tresholds" in r.json()["detail"]["errors"][0]
    r = put(api, {"on_fail": {"scan.secrets": {"pr": "warn"}}})
    assert r.status_code == 422 and "can't be looser" in r.json()["detail"]["errors"][0]


def test_published_policy_is_versioned_and_readable(setup):
    api, _, _ = setup
    version = put(api, THRESHOLD).json()["version"]
    got = api.get("/v1/team-policies/payments-api").json()
    assert got["version"] == version and got["policy"] == THRESHOLD


# ---- Effect on decisions ----


def test_team_threshold_blocks_and_the_decision_records_the_policy_version(setup, conn):
    api, connect, policy = setup
    slow_integration(conn)
    # Before the team policy, the slow load test passes the baseline.
    assert ask(api, "integration")["outcome"] == "PROCEED"

    version = put(api, THRESHOLD).json()["version"]
    d = ask(api, "integration")
    assert d["outcome"] == "BLOCKED" and d["findings"][0]["rule"] == "team.perf.load.threshold"
    assert d["input"]["team_policy_version"] == version
    assert replay(connect, policy, d["decision_id"])


def test_team_extra_checks_are_waited_for(setup, conn):
    api, _, _ = setup
    record(conn, "tests.unit")
    record(conn, "scan.secrets")
    # The team also waits for e2e tests, which never reported.
    put(api, {"extra_checks": {"pr": {"tests.e2e": {"deadline": 0, "on_missing": "validate"}}}})
    d = ask(api, "pr")
    assert d["outcome"] == "REQUIRE_VALIDATION" and "tests.e2e" in d["findings"][0]["message"]


def test_dry_run_backtests_recent_decisions(setup, conn):
    api, _, _ = setup
    slow_integration(conn)
    ask(api, "integration")  # PROCEED today

    bt = put(api, THRESHOLD, dry_run=True).json()["backtest"]
    assert bt["decisions"] == 1 and bt["changed"] == 1
    assert bt["examples"][0]["was"] == "PROCEED" and bt["examples"][0]["would_be"] == "BLOCKED"


# ---- Waivers ----


def test_waiver_downgrades_until_it_expires(setup, conn, now):
    api, _, _ = setup
    slow_integration(conn)
    put(api, THRESHOLD)
    assert waive(api, "team.perf.load.threshold").status_code == 201

    d = ask(api, "integration")
    assert d["outcome"] == "PROCEED_WITH_WARNING" and d["findings"][0]["waiver"] == "PAY-7"

    # A day after the 7-day waiver runs out, the block is back.
    now[0] = T0 + timedelta(days=8)
    assert ask(api, "integration")["outcome"] == "BLOCKED"


def test_security_rules_need_a_security_approver(setup, conn):
    api, _, _ = setup
    assert waive(api, "baseline.scan.sca").status_code == 403
    assert waive(api, "baseline.scan.sca", token="good-token").status_code == 403
    assert waive(api, "baseline.scan.sca", token="security-token").status_code == 201


def test_revoked_waiver_stops_applying(setup, conn):
    api, _, _ = setup
    slow_integration(conn)
    put(api, THRESHOLD)
    waiver_id = waive(api, "team.perf.load.threshold").json()["waiver_id"]

    # A developer can't revoke it; an approver can.
    assert api.delete(f"/v1/waivers/{waiver_id}", headers=AUTH).status_code == 403
    assert (
        api.delete(f"/v1/waivers/{waiver_id}", headers=bearer("approver-token")).status_code == 204
    )
    assert api.get("/v1/waivers", params={"service": "payments-api"}).json() == []
    assert ask(api, "integration")["outcome"] == "BLOCKED"
