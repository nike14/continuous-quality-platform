"""The files in examples/ say what CQP does with them; check they're still right (real OPA)."""

import json
from pathlib import Path

import pytest
import yaml
from conftest import POLICY_DIR
from cqp.decide.policy import Policy
from cqp.decide.team import TeamPolicy
from pydantic import ValidationError

EXAMPLES = Path(__file__).parent.parent / "examples"
ACCEPTED = sorted([EXAMPLES / "team-policy.yaml", *(EXAMPLES / "team-policies").glob("*.yaml")])
REJECTED = sorted((EXAMPLES / "team-policies" / "rejected").glob("*.yaml"))
OPA_EXAMPLES = sorted((EXAMPLES / "opa").glob("*.json"))


@pytest.fixture(scope="module")
def policy(opa_url):
    p = Policy(POLICY_DIR, opa_url)
    p.install()
    return p


# The same two steps as PUT /v1/team-policies: the shape, then the bounds in baseline.rego.
def team_policy_errors(policy, path):
    try:
        team = TeamPolicy.model_validate(yaml.safe_load(path.read_text()))
    except ValidationError as e:
        return [".".join(map(str, x["loc"])) for x in e.errors()]
    return policy.team_errors(team.model_dump(exclude_defaults=True))


@pytest.mark.parametrize("path", ACCEPTED, ids=lambda p: p.name)
def test_example_team_policies_are_accepted(policy, path):
    assert team_policy_errors(policy, path) == []


@pytest.mark.parametrize("path", REJECTED, ids=lambda p: p.name)
def test_rejected_examples_are_rejected_for_the_reasons_they_list(policy, path):
    errors = team_policy_errors(policy, path)
    assert errors
    # Each file's header lists its errors; every one CQP gives must be among them.
    header = "".join(line for line in path.read_text().splitlines() if line.startswith("#"))
    for e in errors:
        assert e in header, e


@pytest.mark.parametrize("path", OPA_EXAMPLES, ids=lambda p: p.stem)
def test_opa_examples_give_what_they_expect(policy, path):
    ex = json.loads(path.read_text())
    if ex["rule"] == "findings":
        got = policy.evaluate(ex["input"])
    else:
        got = sorted(policy._query(ex["rule"], ex["input"]))
    assert got == ex["expect"]
