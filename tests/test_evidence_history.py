"""What CQP learns from earlier results: reruns and flaky tests, quarantine, findings that were
already on the protected branch, and measurements that got worse. Real Postgres and OPA."""

import pytest
from conftest import CLAIMS
from cqp.store.db import save
from test_decide_api import IMAGE, ask, ready_for_prod, record  # noqa: F401 (fixture)
from test_team_and_waivers import put

SHA = CLAIMS["sha"]
MAIN = "a1" * 20  # an earlier commit on the protected branch


def sig(
    conn,
    check,
    status="pass",
    payload=None,
    *,
    commit=SHA,
    image=None,
    run="1",
    at="2026-10-07T09:00:00Z",
    trusted=True,
    kind="outcome",
):
    """A stored signal with every field a test may want to vary."""
    save(
        conn,
        {
            "event_id": f"{check}-{status}-{commit}-{image}-{run}",
            "service": "payments-api",
            "commit": commit,
            "image": image,
            "check": check,
            "kind": kind,
            "status": status,
            "run": run,
            "producer": "ci",
            "payload": payload or {},
            "raw_ref": None,
            "observed_at": at,
            "trusted": trusted,
        },
    )
    conn.commit()


def failing(*tests):
    return {"failed": len(tests), "failing": list(tests)}


def rules(d):
    return {f["rule"]: f for f in d["findings"]}


# ---- Reruns and flaky tests ----


def test_a_check_that_passed_only_on_a_rerun_is_flagged(setup, conn):
    api, _, _ = setup
    sig(conn, "tests.unit", "fail", failing("RefundTest.partial"), run="1")
    sig(conn, "tests.unit", "pass", run="2", at="2026-10-07T09:10:00Z")
    record(conn, "scan.secrets")

    d = ask(api, "pr")

    # The rerun's pass counts, but the earlier failure is shown, not forgotten.
    assert d["outcome"] == "PROCEED_WITH_WARNING"
    f = rules(d)["baseline.passed_on_rerun"]
    assert f["items"] == ["RefundTest.partial"] and "1 earlier run(s)" in f["message"]


def test_a_new_commit_that_passes_is_a_fix_not_a_rerun(setup, conn):
    api, _, _ = setup
    sig(conn, "tests.unit", "fail", failing("RefundTest.partial"), commit=MAIN)
    sig(conn, "tests.unit", "pass", at="2026-10-07T09:10:00Z")
    record(conn, "scan.secrets")
    assert ask(api, "pr")["outcome"] == "PROCEED"


def test_flaky_lists_tests_that_failed_then_passed_on_the_same_commit(setup, conn):
    api, _, _ = setup
    for commit in (SHA, MAIN):
        sig(conn, "tests.unit", "fail", failing("RefundTest.partial"), commit=commit)
        sig(conn, "tests.unit", "pass", commit=commit, run="2", at="2026-10-07T09:10:00Z")
    # Failed and never passed again on its commit: broken, not flaky.
    sig(conn, "tests.unit", "fail", failing("LedgerTest.sum"), commit="c3" * 20)

    found = api.get("/v1/flaky", params={"service": "payments-api"}).json()

    assert [(f["check"], f["test"], f["commits"]) for f in found] == [
        ("tests.unit", "RefundTest.partial", 2)
    ]


# ---- Quarantine ----

QUARANTINE = {"quarantine": {"tests.unit": {"ticket": "QA-12", "tests": ["RefundTest.partial"]}}}


def test_a_quarantined_test_only_warns_but_a_new_failure_still_blocks(setup, conn):
    api, _, _ = setup
    assert put(api, QUARANTINE).status_code == 200
    record(conn, "scan.secrets")
    sig(conn, "tests.unit", "fail", failing("RefundTest.partial"))

    d = ask(api, "pr")
    assert d["outcome"] == "PROCEED_WITH_WARNING"
    assert "(quarantined under QA-12)" in rules(d)["baseline.tests.unit"]["message"]

    sig(
        conn,
        "tests.unit",
        "fail",
        failing("RefundTest.partial", "LedgerTest.sum"),
        run="2",
        at="2026-10-07T09:10:00Z",
    )
    assert ask(api, "pr")["outcome"] == "BLOCKED"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"tests.contract": {"ticket": "QA-1", "tests": ["a"]}}, "can't be quarantined"),
        ({"scan.sca": {"ticket": "QA-1", "tests": ["CVE-1"]}}, "can't be quarantined"),
        ({"tests.e2e": {"ticket": "QA-1", "tests": [f"t{i}" for i in range(11)]}}, "at most 10"),
        ({"tests.e2e": {"tests": ["a"]}}, "ticket"),
    ],
)
def test_quarantine_has_bounds(setup, body, error):
    api, _, _ = setup
    r = put(api, {"quarantine": body}, dry_run=True)
    assert r.status_code == 422 and error in r.text


# ---- New vs existing findings ----


def build_evidence(conn):
    for check in ("tests.unit", "tests.contract", "scan.secrets"):
        record(conn, check)


def cves(*ids):
    return {"by_severity": {"critical": len(ids)}, "failing": list(ids)}


def test_findings_already_on_the_protected_branch_only_warn_before_production(setup, conn):
    api, _, _ = setup
    build_evidence(conn)
    sig(conn, "scan.sca", "fail", cves("CVE-1"), commit=MAIN, kind="findings")
    sig(conn, "scan.sca", "fail", cves("CVE-1"), kind="findings", at="2026-10-07T09:30:00Z")

    d = ask(api, "build")

    assert d["outcome"] == "PROCEED_WITH_WARNING"
    assert "already on the protected branch" in rules(d)["baseline.scan.sca"]["message"]


def test_a_new_finding_keeps_its_effect_and_is_named(setup, conn):
    api, _, _ = setup
    build_evidence(conn)
    sig(conn, "scan.sca", "fail", cves("CVE-1"), commit=MAIN, kind="findings")
    sig(
        conn, "scan.sca", "fail", cves("CVE-1", "CVE-2"), kind="findings", at="2026-10-07T09:30:00Z"
    )

    d = ask(api, "build")

    assert d["outcome"] == "BLOCKED"
    msg = rules(d)["baseline.scan.sca"]["message"]
    assert "new in this change: CVE-2 (1 more already on the protected branch)" in msg


def test_an_unprotected_branch_is_not_a_baseline(setup, conn):
    # Otherwise a branch could make its own findings look old.
    api, _, _ = setup
    build_evidence(conn)
    sig(conn, "scan.sca", "fail", cves("CVE-1"), commit=MAIN, kind="findings", trusted=False)
    sig(conn, "scan.sca", "fail", cves("CVE-1"), kind="findings", at="2026-10-07T09:30:00Z")
    assert ask(api, "build")["outcome"] == "BLOCKED"


def test_secrets_already_on_the_protected_branch_still_block(setup, conn):
    api, _, _ = setup
    for check in ("tests.unit", "tests.contract", "scan.sca"):
        record(conn, check)
    sig(conn, "scan.secrets", "fail", {"failing": ["fp-1"]}, commit=MAIN, kind="findings")
    sig(
        conn,
        "scan.secrets",
        "fail",
        {"failing": ["fp-1"]},
        kind="findings",
        at="2026-10-07T09:30:00Z",
    )
    assert ask(api, "build")["outcome"] == "BLOCKED"


@pytest.mark.usefixtures("ready_for_prod")
def test_production_judges_the_image_as_it_is(setup, conn):
    api, _, _ = setup
    sig(conn, "scan.sca", "fail", cves("CVE-1"), commit=MAIN, kind="findings")
    sig(
        conn,
        "scan.sca",
        "fail",
        cves("CVE-1"),
        image=IMAGE,
        kind="findings",
        run="2",
        at="2026-10-07T09:30:00Z",
    )
    assert ask(api, "prod-deploy", image=IMAGE)["outcome"] == "BLOCKED"


# ---- Measurements against the protected branch ----


def p95(value):
    return {"metric": "p95_ms", "value": value, "unit": "ms"}


def load_test(conn, before, now_):
    record(conn, "tests.integration")
    sig(conn, "perf.load", payload=p95(before), commit=MAIN, kind="measurement")
    sig(conn, "perf.load", payload=p95(now_), kind="measurement", at="2026-10-07T09:30:00Z")


def test_p95_up_more_than_20_percent_warns(setup, conn):
    api, _, _ = setup
    load_test(conn, 100, 130)
    d = ask(api, "integration")
    assert d["outcome"] == "PROCEED_WITH_WARNING"
    assert (
        "30% above the protected branch (100 ms)" in rules(d)["baseline.perf.regression"]["message"]
    )


def test_a_small_change_in_p95_says_nothing(setup, conn):
    api, _, _ = setup
    load_test(conn, 100, 115)
    assert ask(api, "integration")["outcome"] == "PROCEED"


def test_a_team_can_set_its_own_regression_limit_and_effect(setup, conn):
    api, _, _ = setup
    policy = {"thresholds": {"perf.load": {"max_increase_pct": 10, "effect": "validate"}}}
    assert put(api, policy).status_code == 200
    load_test(conn, 100, 115)

    d = ask(api, "integration")

    assert d["outcome"] == "REQUIRE_VALIDATION"
    assert "over the team's limit of +10%" in rules(d)["team.perf.load.regression"]["message"]
    assert "baseline.perf.regression" not in rules(d)  # the team's own limit replaces the default


def test_a_threshold_needs_some_limit(setup):
    api, _, _ = setup
    r = put(api, {"thresholds": {"perf.load": {"effect": "warn"}}}, dry_run=True)
    assert r.status_code == 422 and "max_increase_pct" in r.text
