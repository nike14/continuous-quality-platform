"""The service catalog: who owns a service and who may deploy. Reads the real policy/ files."""

from pathlib import Path

import pytest
from cqp.catalog import Catalog

catalog = Catalog(Path(__file__).parent.parent / "policy")


def test_only_the_listed_repo_owns_a_service():
    assert catalog.owns("payments/payments-api", "payments-api")
    assert not catalog.owns("checkout/checkout-web", "payments-api")
    assert not catalog.owns("payments/payments-api", "not-in-catalog")
    assert not catalog.owns(None, "payments-api")


def test_patterns_cover_throwaway_test_services():
    assert catalog.repo("e2e-svc-123") == "platform/e2e"
    assert catalog.tier("payments-api") == "tier-1"


def test_deployer_needs_its_project_and_a_protected_branch():
    assert catalog.is_deployer({"project_path": "platform/deployer", "ref_protected": "true"})
    assert not catalog.is_deployer({"project_path": "platform/deployer", "ref_protected": "false"})
    assert not catalog.is_deployer(
        {"project_path": "payments/payments-api", "ref_protected": "true"}
    )


def test_mode_defaults_to_enforce_and_reads_the_catalog():
    assert catalog.mode("payments-api") == "enforce"
    assert catalog.mode("checkout-web") == "advisory"


def test_an_unknown_mode_is_refused(tmp_path):
    # A typo must not silently become "enforce" (or worse, switch enforcement off).
    (tmp_path / "catalog.yaml").write_text("svc: {repo: a/b, mode: advsory}\n")
    (tmp_path / "trust.yaml").write_text("deployers: []\n")
    with pytest.raises(ValueError, match="unknown mode"):
        Catalog(tmp_path).mode("svc")
