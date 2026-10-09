"""Kill switches, break-glass and the portal through the Decision API (real Postgres and OPA)."""

from cqp.decide.app import replay
from prometheus_client import REGISTRY
from test_decide_api import AUTH, ask, record


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def blocked_pr(conn, api):
    record(conn, "tests.unit", "fail", {"failed": 1, "failing": ["PaymentRefundTest.partial"]})
    record(conn, "scan.secrets")
    # The starting point for these tests: a PR blocked by a failing unit test.
    d = ask(api, "pr")
    assert d["outcome"] == "BLOCKED"
    return d


def kill(api, token="platform-token", hours=4):
    return api.post(
        "/v1/kill-switches",
        json={"rule": "baseline.tests.unit", "reason": "bad release", "hours": hours},
        headers=bearer(token),
    )


def glass(api, decision_id, token="oncall-token"):
    return api.post(
        f"/v1/decisions/{decision_id}/break-glass",
        json={"reason": "refunds failing in prod", "ticket": "INC-77"},
        headers=bearer(token),
    )


# ---- Kill switches ----


def test_only_the_platform_team_can_switch_a_rule_off(setup):
    api, _, _ = setup
    assert kill(api, token="approver-token").status_code == 403
    assert kill(api, hours=100).status_code == 403


def test_kill_switch_turns_the_rule_into_a_warning_until_cleared(setup, conn):
    api, _, _ = setup
    blocked_pr(conn, api)
    # Switch off the rule that blocked it; the same change now only warns.
    kill_id = kill(api).json()["kill_id"]

    d = ask(api, "pr")
    assert d["outcome"] == "PROCEED_WITH_WARNING" and "switched off" in d["findings"][0]["message"]

    assert api.delete(f"/v1/kill-switches/{kill_id}", headers=AUTH).status_code == 403
    assert (
        api.delete(f"/v1/kill-switches/{kill_id}", headers=bearer("platform-token")).status_code
        == 204
    )
    assert api.get("/v1/kill-switches").json() == []
    assert ask(api, "pr")["outcome"] == "BLOCKED"


# ---- Break-glass ----


def test_developers_cannot_break_glass(setup, conn):
    api, _, _ = setup
    d = blocked_pr(conn, api)
    assert glass(api, d["decision_id"], token="good-token").status_code == 403
    assert glass(api, "nope").status_code == 404


def test_break_glass_lets_the_change_through_and_is_counted(setup, conn):
    api, connect, policy = setup
    d = blocked_pr(conn, api)
    # Read the counter first, so the check holds whatever ran before.
    before = REGISTRY.get_sample_value("cqp_break_glass_total", {"stage": "pr"}) or 0
    assert glass(api, d["decision_id"]).status_code == 201

    again = ask(api, "pr")
    assert again["outcome"] == "PROCEED_WITH_WARNING"
    assert {f["rule"] for f in again["findings"]} == {"baseline.tests.unit", "baseline.break_glass"}
    assert REGISTRY.get_sample_value("cqp_break_glass_total", {"stage": "pr"}) == before + 1
    assert replay(connect, policy, again["decision_id"])


def test_break_glass_does_not_wait_for_slow_evidence(setup, conn):
    api, _, _ = setup
    d = ask(api, "integration")  # nothing reported yet: normally pending for up to 2 hours
    assert d["status"] == "pending"
    glass(api, d["decision_id"])

    d = ask(api, "integration")
    assert d["status"] == "final" and d["outcome"] == "PROCEED_WITH_WARNING"


# ---- Portal ----


def test_portal_lists_waivers_and_escapes_what_people_typed(setup, conn):
    api, _, _ = setup
    api.post(
        "/v1/waivers",
        headers=bearer("approver-token"),
        json={
            "service": "payments-api",
            "rule": "baseline.tests.e2e",
            "days": 3,
            "reason": "<script>alert(1)</script>",
            "ticket": "PAY-9",
        },
    )
    # The reason holds a script tag; the page must show it as text, not run it.
    page = api.get("/portal")
    assert page.status_code == 200 and "PAY-9" in page.text and "payments/payments-api" in page.text
    assert "<script>alert(1)" not in page.text and "&lt;script&gt;" in page.text
