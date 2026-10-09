"""The service catalog (policy/catalog.yaml) and who may speak for which service."""

from fnmatch import fnmatch
from pathlib import Path

import yaml


class Catalog:
    """Reads policy/catalog.yaml and policy/trust.yaml."""

    def __init__(self, folder: Path):
        self.services = yaml.safe_load((folder / "catalog.yaml").read_text())
        trust = yaml.safe_load((folder / "trust.yaml").read_text())
        self.deployers = set(trust["deployers"])

    def get(self, service: str) -> dict:
        """The catalog entry for a service, or {} if it isn't listed."""
        if service in self.services:
            return self.services[service]
        # Patterns only for throwaway test services, e.g. "e2e-svc-*".
        return next((v for k, v in self.services.items() if fnmatch(service, k)), {})

    def tier(self, service: str) -> str:
        """The service's tier; unlisted services get the default rules."""
        return self.get(service).get("tier", "default")

    def repo(self, service: str) -> str | None:
        """The repo that owns the service, or None."""
        return self.get(service).get("repo")

    def owns(self, project: str | None, service: str) -> bool:
        """A repo may report for, gate on and set policy for only the services it owns."""
        return project is not None and project == self.repo(service)

    def is_deployer(self, claims: dict) -> bool:
        """Only the deploy system, from a protected branch, may ask about production."""
        # GitLab sends claim values as strings ("true"), hence the text comparison.
        return (
            claims.get("project_path") in self.deployers
            and str(claims.get("ref_protected")).lower() == "true"
        )
