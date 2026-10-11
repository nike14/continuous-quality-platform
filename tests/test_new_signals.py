"""Event schemas and database migrations, judged by the baseline. Real Postgres and OPA."""

from test_decide_api import ask
from test_evidence_history import rules, sig
from test_team_and_waivers import put

BUILD_EXPECTED = ("tests.unit", "tests.contract", "scan.sca", "scan.secrets")


def build_with(conn, check, status, payload, kind):
    for c in BUILD_EXPECTED:
        sig(conn, c)
    sig(conn, check, status, payload, kind=kind, at="2026-10-07T09:30:00Z")


def test_an_incompatible_event_schema_blocks_the_build(setup, conn):
    api, _, _ = setup
    build_with(conn, "contract.schema", "fail", {"failing": ["orders-value"]}, "outcome")
    d = ask(api, "build")
    assert d["outcome"] == "BLOCKED"
    assert "orders-value" in rules(d)["baseline.contract.schema"]["message"]


def test_a_destructive_migration_blocks_the_build(setup, conn):
    api, _, _ = setup
    failing = ["ban-drop-column migrations/0042_orders.sql:3"]
    build_with(conn, "db.migration", "fail", {"failing": failing, "locking": []}, "findings")
    d = ask(api, "build")
    assert d["outcome"] == "BLOCKED"
    assert rules(d)["baseline.db.migration"]["items"] == failing


def test_a_migration_that_takes_long_locks_needs_a_person(setup, conn):
    api, _, _ = setup
    locks = ["require-concurrent-index-creation migrations/0042_orders.sql:7"]
    build_with(conn, "db.migration", "pass", {"failing": [], "locking": locks}, "findings")
    d = ask(api, "build")
    assert d["outcome"] == "REQUIRE_VALIDATION"
    assert "takes long locks" in rules(d)["baseline.db.locking"]["message"]


def test_locks_only_warn_on_a_pr(setup, conn):
    # Not judged at pr: a developer sees them in the build stage, where a person can look.
    api, _, _ = setup
    sig(conn, "tests.unit")
    sig(conn, "scan.secrets")
    sig(conn, "db.migration", payload={"failing": [], "locking": ["x m.sql:1"]}, kind="findings")
    assert "baseline.db.locking" not in rules(ask(api, "pr"))


def test_a_team_cannot_loosen_the_new_locked_checks(setup):
    api, _, _ = setup
    body = {
        "on_fail": {"contract.schema": {"build": "warn"}, "db.migration": {"prod-deploy": "warn"}}
    }
    r = put(api, body, dry_run=True)
    assert r.status_code == 422
    assert "contract.schema at build can't be looser" in r.text
    assert "db.migration at prod-deploy can't be looser" in r.text


def test_a_team_can_wait_for_them(setup, conn):
    # Not every service has events or a database, so they're opt-in, like other checks.
    api, _, _ = setup
    policy = {"extra_checks": {"build": {"db.migration": {"deadline": 0, "on_missing": "block"}}}}
    assert put(api, policy).status_code == 200
    for c in BUILD_EXPECTED:
        sig(conn, c)
    d = ask(api, "build")
    assert d["outcome"] == "BLOCKED"
    assert "db.migration did not report" in rules(d)["baseline.evidence_required"]["message"]
