"""Team policy files (.quality/policy.yaml).

The shape is checked here; the bounds in baseline.rego.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cqp.gather.event import Check

Stage = Literal["pr", "build", "integration", "prod-deploy"]
Effect = Literal["warn", "validate", "block"]


class Expectation(BaseModel):
    """An extra check to wait for: how long, and what happens if it never reports."""

    model_config = ConfigDict(extra="forbid")
    deadline: int = Field(ge=0)  # minutes
    on_missing: Effect


class Threshold(BaseModel):
    """A team's limit on a measurement, and the effect when it's crossed.

    max is absolute (p95 at most 150 ms); max_increase_pct is relative to the protected
    branch's last value (p95 at most 10% worse). Either or both.
    """

    model_config = ConfigDict(extra="forbid")
    max: float | None = None
    max_increase_pct: float | None = Field(default=None, gt=0)
    effect: Effect

    @model_validator(mode="after")
    def some_limit(self) -> "Threshold":
        """A threshold with no limit would silently do nothing."""
        if self.max is None and self.max_increase_pct is None:
            raise ValueError("set max, max_increase_pct or both")
        return self


class Quarantine(BaseModel):
    """Known-flaky tests whose failure only warns while the ticket is worked on."""

    model_config = ConfigDict(extra="forbid")
    ticket: str = Field(min_length=1)
    tests: list[str] = Field(min_length=1)  # names as in the finding, e.g. "Class.test"


class TeamPolicy(BaseModel):
    """What a team may set. Unknown keys are rejected so typos don't silently do nothing."""

    model_config = ConfigDict(extra="forbid")
    extra_checks: dict[Stage, dict[Check, Expectation]] = {}  # more checks to wait for
    on_fail: dict[Check, dict[Stage, Effect]] = {}  # effect of a failing check
    thresholds: dict[Check, Threshold] = {}  # limits on measurements
    quarantine: dict[Check, Quarantine] = {}  # flaky tests that only warn; bounds in Rego
