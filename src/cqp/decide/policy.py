"""The pinned policy: Rego baseline (evaluated by an OPA sidecar), expectations and catalog."""

import hashlib
from datetime import timedelta
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
        self._opa = httpx.Client(base_url=opa_url, timeout=2)  # a local sidecar; slower = trouble
        self._installed_versions: set[str] = set()

    def install(self) -> None:
        """Loads our Rego into the OPA sidecar."""
        # Pushed at startup, so OPA always runs exactly the Rego we hashed into self.version.
        self._opa.put("/v1/policies/cqp-baseline", content=self.rego).raise_for_status()

    def tier(self, service: str) -> str:
        """The service's tier, from the catalog."""
        return self.catalog.tier(service)

    def max_age(self, check: str) -> timedelta | None:
        """How long a result for this check stays usable, or None if it doesn't expire."""
        days = self.expectations.get("max_age_days", {}).get(check)
        return timedelta(days=days) if days is not None else None

    def expected(self, stage: str, tier: str) -> dict:
        """The checks a stage waits for at this tier, with their deadlines."""
        by_tier = self.expectations[stage]
        return by_tier.get(tier, by_tier["default"])

    def _query(
        self, rule: str, policy_input: dict, version: str | None = None, rego: str = ""
    ) -> list:
        """Evaluates one rule in OPA (current rules, or a stored version) and returns its result."""
        old = version if version is not None and version != self.version else None
        package = f"v{old}" if old else "baseline"
        for attempt in range(2):
            if old:
                self._install_version(old, rego)
            r = self._opa.post(f"/v1/data/cqp/{package}/{rule}", json={"input": policy_input})
            r.raise_for_status()
            body = r.json()
            # A loaded rule always has a result, even an empty one. None means OPA has lost our
            # rules (it restarted): reading that as "no findings" would let everything proceed.
            if "result" in body:
                return body["result"]
            if attempt == 0:
                self.install()
                self._installed_versions.clear()
        raise RuntimeError(f"OPA has no cqp/{package}/{rule} rule loaded")

    def _install_version(self, version: str, rego: str) -> None:
        """Loads an older baseline into OPA under its own package (once)."""
        # Its own package, so it sits beside the current rules instead of clashing with them.
        if version not in self._installed_versions:
            module = rego.replace("package cqp.baseline", f"package cqp.v{version}", 1)
            self._opa.put(f"/v1/policies/cqp-v{version}", content=module).raise_for_status()
            self._installed_versions.add(version)

    def evaluate(
        self, policy_input: dict, version: str | None = None, rego: str = ""
    ) -> list[dict]:
        """The findings for one decision's input, under the current rules or a stored version."""
        found = self._query("findings", policy_input, version, rego)
        # Sorted, so the same input always gives the same list (stable MR comments, easy replay).
        return sorted(found, key=lambda f: f["rule"])

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
