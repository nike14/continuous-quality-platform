"""Who and what CQP trusts, and the ways past a decision: the in-flight grace per job, evidence
from unprotected branches, approvals, previews and baseline backtests. Real Postgres and OPA."""

from datetime import timedelta

import httpx
import pytest
from conftest import POLICY_DIR, T0
from cqp.decide.app import replay
from cqp.decide.gate import FALLBACK_BLOCKING
from test_decide_api import IMAGE, ask, ready_for_prod, record  # noqa: F401 (fixture)
from test_evidence_history import rules, sig
from test_team_and_waivers import bearer, put

# ---- The caller's own uploads ----


def test_a_retried_job_gets_its_own_minute_for_its_uploads(setup, conn, now):
    # Regression: the grace counted from the first job ever, so a pipeline retried an hour later
    # was decided before its own new uploads had landed.
    api, _, _ = setup
    record(conn, "tests.unit")
    record(conn, "scan.secrets")
    assert ask(api, "pr")["status"] == "final"

    now[0] = T0 + timedelta(hours=1)
    retried = {"Authorization": "Bearer retried-job-token"}
    body = {"service": "payments-api", "stage": "pr", "event_ids": ["still-in-kafka"]}
    d = api.post("/v1/decisions", json=body, headers=retried).json()
    assert d["status"] == "pending" and d["waiting"] == [{"event_id": "still-in-kafka"}]

    now[0] = T0 + timedelta(hours=1, minutes=2)  # that job's own minute is over
    assert api.post("/v1/decisions", json=body, headers=retried).json()["status"] == "final"


# ---- Evidence from unprotected branches ----

OTHER = "b2" * 20  # a branch commit claiming to be about IMAGE


@pytest.mark.usefixtures("ready_for_prod")
def test_an_untrusted_pass_about_the_image_is_ignored_in_production(setup, conn):
    api, _, _ = setup
    sig(
        conn,
        "scan.sca",
        "fail",
        {"failing": ["CVE-1"]},
        image=IMAGE,
        run="2",
        at="2026-10-07T09:10:00Z",
        kind="findings",
    )
    # A branch job uploads a clean scan "for" the same image, later.
    sig(
        conn,
        "scan.sca",
        "pass",
        commit=OTHER,
        image=IMAGE,
        run="3",
        at="2026-10-07T09:20:00Z",
        kind="findings",
        trusted=False,
    )

    d = ask(api, "prod-deploy", image=IMAGE)

    assert d["outcome"] == "BLOCKED" and "CVE-1" in rules(d)["baseline.scan.sca"]["message"]


@pytest.mark.usefixtures("ready_for_prod")
def test_an_untrusted_failure_about_the_image_still_counts(setup, conn):
    api, _, _ = setup
    sig(
        conn,
        "scan.sca",
        "fail",
        {"failing": ["CVE-1"]},
        commit=OTHER,
        image=IMAGE,
        run="3",
        at="2026-10-07T09:20:00Z",
        kind="findings",
        trusted=False,
    )
    assert ask(api, "prod-deploy", image=IMAGE)["outcome"] == "BLOCKED"


def test_untrusted_provenance_does_not_link_an_image_to_a_commit(setup, conn):
    api, _, _ = setup
    record(conn, "tests.unit")
    sig(conn, "build.provenance", image="sha256:forged", trusted=False)

    d = ask(api, "prod-deploy", image="sha256:forged")

    assert "tests.unit" not in d["input"]["evidence"]  # the commit's evidence isn't inherited
    assert d["outcome"] == "BLOCKED"  # and provenance itself is missing


# ---- Approvals ----

VALIDATE = {"thresholds": {"perf.load": {"max": 100, "effect": "validate"}}}


@pytest.fixture
def needs_validation(setup, conn):
    """A final integration decision that needs a person: p95 over the team's limit."""
    api, _, _ = setup
    assert put(api, VALIDATE).status_code == 200
    record(conn, "tests.integration")
    sig(
        conn,
        "perf.load",
        payload={"metric": "p95_ms", "value": 200, "unit": "ms"},
        kind="measurement",
    )
    d = ask(api, "integration")
    assert d["status"] == "final" and d["outcome"] == "REQUIRE_VALIDATION"
    return d


def approve(api, decision_id, token="approver-token"):
    return api.post(
        f"/v1/decisions/{decision_id}/approvals",
        json={"reason": "p95 is a known cold-cache spike", "ticket": "PERF-3"},
        headers=bearer(token),
    )


def test_an_approval_accepts_what_needed_validation(setup, needs_validation):
    api, connect, policy = setup
    r = approve(api, needs_validation["decision_id"])
    assert r.status_code == 201
    assert [f["rule"] for f in r.json()["findings"]] == ["team.perf.load.threshold"]

    d = ask(api, "integration")  # the re-run gate job

    assert d["outcome"] == "PROCEED_WITH_WARNING"
    assert "(approved by lead: p95 is a known cold-cache spike)" in d["findings"][0]["message"]
    assert replay(connect, policy, d["decision_id"])  # the approval is part of the input


def test_you_cannot_approve_your_own_change(setup, needs_validation):
    api, _, _ = setup
    r = approve(api, needs_validation["decision_id"], token="dev-approver-token")
    assert r.status_code == 403 and "you asked for" in r.text


@pytest.mark.parametrize(
    ("token", "error"),
    [("good-token", "only approvers"), ("other-approver-token", "doesn't own payments-api")],
)
def test_only_an_approver_of_the_service_can_approve(setup, needs_validation, token, error):
    api, _, _ = setup
    r = approve(api, needs_validation["decision_id"], token=token)
    assert r.status_code == 403 and error in r.text


def test_a_blocked_or_pending_decision_cannot_be_approved(setup, conn):
    api, _, _ = setup
    record(conn, "tests.unit", "fail", {"failed": 1, "failing": ["A.b"]})
    record(conn, "scan.secrets")
    blocked = ask(api, "pr")
    assert approve(api, blocked["decision_id"]).status_code == 409
    record(conn, "tests.integration")
    pending = ask(api, "integration")  # perf.load hasn't reported yet
    assert pending["status"] == "pending"
    assert approve(api, pending["decision_id"]).status_code == 409


def test_an_approval_does_not_cover_a_different_finding(setup, conn, needs_validation):
    api, _, _ = setup
    approve(api, needs_validation["decision_id"])
    # A new load test run, worse again: not what the approver looked at.
    sig(
        conn,
        "perf.load",
        payload={"metric": "p95_ms", "value": 400, "unit": "ms"},
        run="2",
        at="2026-10-07T09:30:00Z",
        kind="measurement",
    )
    assert ask(api, "integration")["outcome"] == "REQUIRE_VALIDATION"


def test_an_approval_lasts_a_day(setup, now, needs_validation):
    api, _, _ = setup
    approve(api, needs_validation["decision_id"])
    now[0] = T0 + timedelta(hours=25)
    assert ask(api, "integration")["outcome"] == "REQUIRE_VALIDATION"


# ---- Preview ----


def test_preview_judges_local_results_and_records_nothing(setup, conn, published):
    api, _, _ = setup
    body = {
        "service": "payments-api",
        "stage": "pr",
        "evidence": {
            "tests.unit": {"kind": "outcome", "status": "fail", "payload": {"failing": ["A.b"]}}
        },
    }
    r = api.post("/v1/preview", json=body)  # no token: nothing is recorded
    assert r.status_code == 200
    assert r.json()["outcome"] == "BLOCKED" and r.json()["not_checked"] == ["scan.secrets"]
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0 and not published


@pytest.mark.parametrize(
    "change",
    [{"stage": "prod-deploy"}, {"evidence": {"tests.made-up": {"kind": "x", "status": "pass"}}}],
)
def test_preview_refuses_production_and_unknown_checks(setup, change):
    api, _, _ = setup
    body = {"service": "payments-api", "stage": "pr", "evidence": {}, **change}
    assert api.post("/v1/preview", json=body).status_code == 422


# ---- Baseline backtest ----


def backtest(api, rego, token="platform-token"):
    return api.post("/v1/baseline/backtest", json={"rego": rego}, headers=bearer(token))


def test_a_baseline_change_is_backtested_on_recent_decisions(setup, conn):
    api, _, _ = setup
    record(conn, "tests.unit", "fail", {"failed": 1, "failing": ["A.b"]})
    record(conn, "scan.secrets")
    assert ask(api, "pr")["outcome"] == "BLOCKED"
    rego = (POLICY_DIR / "baseline.rego").read_text()
    softer = rego.replace('"tests.unit": {"pr": "block"', '"tests.unit": {"pr": "warn"', 1)
    assert softer != rego

    r = backtest(api, softer)

    assert r.status_code == 200
    bt = r.json()
    assert bt["changed"] == 1 and bt["examples"][0]["would_be"] == "PROCEED_WITH_WARNING"
    assert backtest(api, rego).json()["changed"] == 0  # the current rules change nothing


@pytest.mark.parametrize(
    ("rego", "token", "status"),
    [
        ("package cqp.baseline\nthis is not rego", "platform-token", 422),
        ("package something.else\n", "platform-token", 422),
        ("package cqp.baseline\n", "approver-token", 403),
    ],
)
def test_a_baseline_backtest_needs_valid_rego_and_the_platform_team(
    setup, conn, rego, token, status
):
    api, _, _ = setup
    record(conn, "tests.unit")
    record(conn, "scan.secrets")
    ask(api, "pr")  # at least one decision, so bad Rego is actually evaluated
    assert backtest(api, rego, token).status_code == status


# ---- The gate's fallback agrees with the baseline ----


def test_the_fallback_blocks_exactly_what_the_baseline_always_blocks(setup, opa_url):
    # The gate can't ask CQP then, so it keeps its own copy of this list; this keeps them equal.
    on_fail = httpx.get(f"{opa_url}/v1/data/cqp/baseline/on_fail").json()["result"]
    always = {check for check, by_stage in on_fail.items() if set(by_stage.values()) == {"block"}}
    assert always == FALLBACK_BLOCKING
