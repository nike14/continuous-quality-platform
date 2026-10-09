"""Pure decision logic: is the evidence complete, and which outcome do the findings add up to."""

from datetime import datetime, timedelta

EFFECTS = ["proceed", "warn", "validate", "block"]
OUTCOMES = ["PROCEED", "PROCEED_WITH_WARNING", "REQUIRE_VALIDATION", "BLOCKED"]


def completeness(expected: dict, evidence: dict, first_asked: datetime, now: datetime):
    """Splits expected checks that haven't reported into still-waiting and missing."""
    waiting, missing = [], []
    for check, rule in expected.items():
        if check in evidence:
            continue
        # Counted from the first time anyone asked, so asking again doesn't buy more time.
        deadline = first_asked + timedelta(minutes=rule["deadline"])
        if now < deadline:
            waiting.append({"check": check, "deadline": deadline.isoformat()})
        else:
            missing.append({"check": check, "on_missing": rule["on_missing"]})
    return waiting, missing


def outcome(findings: list[dict]) -> str:
    """Turns a list of findings into one of the four outcomes."""
    # The strictest finding wins; no findings means proceed.
    return OUTCOMES[max((EFFECTS.index(f["effect"]) for f in findings), default=0)]
