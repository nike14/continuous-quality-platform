"""Baseline rules evaluated by a real OPA (the `opa_url` fixture in conftest)."""

import pytest
from conftest import POLICY_DIR
from cqp.decide.policy import Policy

UNIT_FAIL = {
    "tests.unit": {
        "kind": "outcome",
        "status": "fail",
        "payload": {"failed": 1, "failing": ["PaymentRefundTest.partial"]},
    }
}


@pytest.fixture(scope="module")
def policy(opa_url):
    p = Policy(POLICY_DIR, opa_url)
    p.install()
    return p


# Asks OPA directly, with just the parts of the input a test cares about.
def run(policy, stage, evidence=None, missing=None):
    return policy.evaluate({"stage": stage, "evidence": evidence or {}, "missing": missing or []})


# ---- Baseline rules ----


def test_failing_unit_tests_block_a_pr_and_name_the_tests(policy):
    [f] = run(policy, "pr", UNIT_FAIL)
    assert f["effect"] == "block" and f["rule"] == "baseline.tests.unit"
    assert "PaymentRefundTest.partial" in f["message"]


def test_effect_depends_on_stage(policy):
    contract_fail = {"tests.contract": {"kind": "outcome", "status": "fail", "payload": {}}}
    assert run(policy, "pr", contract_fail)[0]["effect"] == "warn"
    assert run(policy, "build", contract_fail)[0]["effect"] == "block"


def test_check_not_judged_at_a_stage_gives_no_finding(policy):
    assert run(policy, "prod-deploy", UNIT_FAIL) == []


def test_missing_evidence_uses_its_on_missing_effect(policy):
    [f] = run(policy, "prod-deploy", missing=[{"check": "scan.sca", "on_missing": "block"}])
    assert f["rule"] == "baseline.evidence_required" and f["effect"] == "block"


def test_passing_evidence_gives_no_findings(policy):
    assert (
        run(policy, "pr", {"tests.unit": {"kind": "outcome", "status": "pass", "payload": {}}})
        == []
    )


def test_expectations_fall_back_to_default_tier(policy):
    assert "tests.contract" in policy.expected("prod-deploy", "tier-1")
    assert "tests.contract" not in policy.expected("prod-deploy", "tier-3")
    assert policy.tier("payments-api") == "tier-1" and policy.tier("unknown") == "default"


# ---- Production state (prod-deploy only) ----

# A healthy production. Each test changes one thing.
HEALTHY = {
    "error_budget_remaining": 0.4,
    "health_fresh": True,
    "incident": None,
    "rolled_back_recently": False,
    "freeze": False,
}


def prod(policy, fix=False, tier="tier-1", **runtime):
    return [
        (f["rule"], f["effect"])
        for f in policy.evaluate(
            {
                "stage": "prod-deploy",
                "tier": tier,
                "evidence": {},
                "missing": [],
                "runtime": {**HEALTHY, **runtime},
                "fix": fix,
            }
        )
    ]


def test_healthy_production_adds_no_findings(policy):
    assert prod(policy) == []


def test_no_error_budget_or_an_incident_needs_approval_unless_it_is_a_fix(policy):
    assert prod(policy, error_budget_remaining=0) == [("baseline.error_budget", "validate")]
    assert prod(policy, incident="sev1") == [("baseline.active_incident", "validate")]
    assert prod(policy, fix=True, error_budget_remaining=0, incident="sev1") == []


def test_recent_rollback_warns_and_a_freeze_blocks(policy):
    assert prod(policy, rolled_back_recently=True) == [("baseline.recent_rollback", "warn")]
    assert prod(policy, freeze=True, fix=True) == [("baseline.change_freeze", "block")]


def test_stale_health_needs_approval_for_tier_1_only(policy):
    assert prod(policy, health_fresh=False) == [("baseline.production_health", "validate")]
    assert prod(policy, tier="default", health_fresh=False) == [
        ("baseline.production_health", "warn")
    ]


# ---- Team policies and waivers ----

SCA_FAIL = {
    "scan.sca": {"kind": "findings", "status": "fail", "payload": {"by_severity": {"critical": 1}}}
}
SLOW = {
    "perf.load": {
        "kind": "measurement",
        "status": "pass",
        "payload": {"metric": "p95_ms", "value": 900, "unit": "ms"},
    }
}


def team_run(policy, stage, evidence, team=None, waivers=None, missing=None):
    return policy.evaluate(
        {
            "stage": stage,
            "evidence": evidence,
            "missing": missing or [],
            "team": team or {},
            "waivers": waivers or [],
        }
    )


def test_team_can_change_an_unlocked_cell(policy):
    [f] = team_run(policy, "pr", UNIT_FAIL, {"on_fail": {"tests.unit": {"pr": "warn"}}})
    assert f["effect"] == "warn"


def test_team_cannot_loosen_a_locked_cell_and_is_told_why(policy):
    team = {"on_fail": {"scan.sca": {"build": "warn"}}}
    assert team_run(policy, "build", SCA_FAIL, team)[0]["effect"] == "block"
    assert policy.team_errors(team) == [
        "scan.sca at build can't be looser than the organisation baseline (block)"
    ]


def test_team_can_tighten_anything(policy):
    team = {"on_fail": {"scan.sca": {"pr": "block"}}}
    assert team_run(policy, "pr", SCA_FAIL, team)[0]["effect"] == "block"
    assert policy.team_errors(team) == []


def test_team_threshold_fires_over_the_limit(policy):
    team = {"thresholds": {"perf.load": {"max": 500, "effect": "validate"}}}
    [f] = team_run(policy, "integration", SLOW, team)
    assert f["rule"] == "team.perf.load.threshold" and f["effect"] == "validate"
    assert "900" in f["message"] and "500" in f["message"]
    # Under the limit: no finding.
    assert (
        team_run(
            policy,
            "integration",
            SLOW,
            {"thresholds": {"perf.load": {"max": 1000, "effect": "block"}}},
        )
        == []
    )


WAIVER = {
    "rule": "baseline.scan.sca",
    "ticket": "SEC-42",
    "expires_at": "2026-10-20T00:00:00+00:00",
}


def test_a_waiver_turns_a_finding_into_a_visible_warning(policy):
    [f] = team_run(policy, "build", SCA_FAIL, waivers=[WAIVER])
    assert f["effect"] == "warn" and f["waiver"] == "SEC-42" and "waived until" in f["message"]


def test_missing_evidence_cannot_be_waived(policy):
    waiver = {**WAIVER, "rule": "baseline.evidence_required"}
    [f] = team_run(
        policy,
        "build",
        {},
        waivers=[waiver],
        missing=[{"check": "scan.sca", "on_missing": "block"}],
    )
    assert f["effect"] == "block" and "waiver" not in f


@pytest.mark.parametrize(
    "rule, role, days, error",
    [
        ("baseline.change_freeze", "security", 5, "this rule can't be waived; use break-glass"),
        ("baseline.tests.e2e", None, 5, "only approvers can create waivers"),
        ("baseline.scan.sca", "approver", 5, "baseline security rules need a security approver"),
        ("baseline.tests.e2e", "approver", 45, "a waiver lasts at most 30 days"),
    ],
)
def test_waiver_requests_are_checked(policy, rule, role, days, error):
    assert error in policy.waiver_errors({"rule": rule, "days": days}, role)


def test_a_valid_waiver_request_has_no_errors(policy):
    assert policy.waiver_errors({"rule": "baseline.scan.sca", "days": 14}, "security") == []


# ---- Kill switches and break-glass ----

FREEZE = {**HEALTHY, "freeze": True}


def test_a_switched_off_rule_only_warns_and_says_why(policy):
    kill = [{"rule": "baseline.tests.unit", "reason": "bad release 1.4"}]
    [f] = policy.evaluate(
        {"stage": "pr", "evidence": UNIT_FAIL, "missing": [], "kill_switches": kill}
    )
    assert f["effect"] == "warn" and "rule switched off: bad release 1.4" in f["message"]


def test_break_glass_lets_everything_through_but_keeps_findings_visible(policy):
    glass = {"by": "sre", "reason": "payments down", "ticket": "INC-9"}
    # A failed scan and a freeze, both overridden by break-glass.
    found = policy.evaluate(
        {
            "stage": "prod-deploy",
            "tier": "tier-1",
            "evidence": SCA_FAIL,
            "missing": [],
            "runtime": FREEZE,
            "break_glass": glass,
        }
    )
    assert {f["rule"] for f in found} == {
        "baseline.scan.sca",
        "baseline.change_freeze",
        "baseline.break_glass",
    }
    assert all(f["effect"] == "warn" for f in found)
    assert (
        "overridden by break-glass"
        in next(f for f in found if f["rule"] == "baseline.scan.sca")["message"]
    )


def test_kill_switch_and_break_glass_requests_are_checked(policy):
    # Each kind of request has its own role and time limit.
    assert policy.kill_errors({"rule": "x", "hours": 4}, "approver") == [
        "only the platform team can switch off rules"
    ]
    assert policy.kill_errors({"rule": "x", "hours": 100}, "platform") == [
        "a kill switch lasts at most 72 hours"
    ]
    assert policy.break_glass_errors(None) != [] and policy.break_glass_errors("oncall") == []
