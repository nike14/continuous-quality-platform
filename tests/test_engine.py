"""Completeness and outcomes: the pure decision logic, no database or OPA."""

from datetime import UTC, datetime, timedelta

from cqp.decide.engine import completeness, outcome

T0 = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
# One check that must already be there, and one that may take two hours (like a load test).
EXPECTED = {
    "tests.unit": {"deadline": 0, "on_missing": "block"},
    "perf.load": {"deadline": 120, "on_missing": "warn"},
}


def test_reported_checks_are_neither_waiting_nor_missing():
    waiting, missing = completeness(EXPECTED, {"tests.unit": {}, "perf.load": {}}, T0, T0)
    assert waiting == [] and missing == []


def test_check_is_waiting_until_its_deadline_then_missing():
    waiting, missing = completeness(EXPECTED, {"tests.unit": {}}, T0, T0 + timedelta(minutes=119))
    assert [w["check"] for w in waiting] == ["perf.load"] and missing == []

    waiting, missing = completeness(EXPECTED, {"tests.unit": {}}, T0, T0 + timedelta(minutes=120))
    assert waiting == [] and missing == [{"check": "perf.load", "on_missing": "warn"}]


def test_deadline_zero_means_it_must_already_be_there():
    _, missing = completeness(EXPECTED, {"perf.load": {}}, T0, T0)
    assert missing == [{"check": "tests.unit", "on_missing": "block"}]


def test_strictest_finding_wins():
    assert outcome([]) == "PROCEED"
    assert outcome([{"effect": "warn"}]) == "PROCEED_WITH_WARNING"
    assert outcome([{"effect": "warn"}, {"effect": "block"}, {"effect": "validate"}]) == "BLOCKED"
