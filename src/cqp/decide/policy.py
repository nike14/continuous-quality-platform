"""The pinned policy: Rego baseline (evaluated by an OPA sidecar), expectations and catalog."""

import hashlib
from pathlib import Path

import httpx
import yaml

from cqp.catalog import Catalog


class Policy:
    """The rules CQP decides with: baseline.rego in OPA, plus expectations and the catalog."""

    def __init__(self, folder: Path, opa_url: str):
        self.rego = (folder / "baseline.rego").read_text()
        expectations = (folder / "expectations.yaml").read_text()
        self.expectations = yaml.safe_load(expectations)
        self.catalog = Catalog(folder)
        # Recorded on every decision, so a replay knows exactly which rules made it.
        self.version = hashlib.sha256((self.rego + expectations).encode()).hexdigest()[:12]
        self._opa = httpx.Client(
            base_url=opa_url, timeout=2
        )  # a local sidecar; slower means trouble

    def install(self) -> None:
        """Loads our Rego into the OPA sidecar."""
        # Pushed at startup, so OPA always runs exactly the Rego we hashed into self.version.
        self._opa.put("/v1/policies/cqp-baseline", content=self.rego).raise_for_status()

    def tier(self, service: str) -> str:
        """The service's tier, from the catalog."""
        return self.catalog.tier(service)

    def expected(self, stage: str, tier: str) -> dict:
        """The checks a stage waits for at this tier, with their deadlines."""
        by_tier = self.expectations[stage]
        return by_tier.get(tier, by_tier["default"])

    def _query(self, rule: str, policy_input: dict) -> list:
        """Evaluates one rule in OPA and returns its result."""
        r = self._opa.post(f"/v1/data/cqp/baseline/{rule}", json={"input": policy_input})
        r.raise_for_status()
        return r.json().get("result", [])

    def evaluate(self, policy_input: dict) -> list[dict]:
        """The findings for one decision's input."""
        # Sorted, so the same input always gives the same list (stable MR comments, easy replay).
        return sorted(self._query("findings", policy_input), key=lambda f: f["rule"])

    def team_errors(self, team: dict) -> list[str]:
        """Why a team policy file is outside the organisation's bounds (empty when it's fine)."""
        return sorted(self._query("team_policy_errors", {"team": team}))

    def waiver_errors(self, waiver: dict, role: str | None) -> list[str]:
        """Why this person can't create this waiver (empty when they can)."""
        return sorted(self._query("waiver_errors", {"waiver": waiver, "role": role}))

    def kill_errors(self, kill: dict, role: str | None) -> list[str]:
        """Why this person can't switch this rule off (empty when they can)."""
        return sorted(self._query("kill_errors", {"kill": kill, "role": role}))

    def break_glass_errors(self, role: str | None) -> list[str]:
        """Why this person can't break glass (empty when they can)."""
        return sorted(self._query("break_glass_errors", {"role": role}))
