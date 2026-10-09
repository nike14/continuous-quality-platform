"""Team policy files (.quality/policy.yaml).

The shape is checked here; the bounds in baseline.rego.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cqp.gather.event import Check

Stage = Literal["pr", "build", "integration", "prod-deploy"]
Effect = Literal["warn", "validate", "block"]


class Expectation(BaseModel):
    """An extra check to wait for: how long, and what happens if it never reports."""

    model_config = ConfigDict(extra="forbid")
    deadline: int = Field(ge=0)  # minutes
    on_missing: Effect


class Threshold(BaseModel):
    """A team's limit on a measurement, and the effect when it's crossed."""

    model_config = ConfigDict(extra="forbid")
    max: float
    effect: Effect


class TeamPolicy(BaseModel):
    """What a team may set. Unknown keys are rejected so typos don't silently do nothing."""

    model_config = ConfigDict(extra="forbid")
    extra_checks: dict[Stage, dict[Check, Expectation]] = {}  # more checks to wait for
    on_fail: dict[Check, dict[Stage, Effect]] = {}  # effect of a failing check
    thresholds: dict[Check, Threshold] = {}  # limits on measurements
