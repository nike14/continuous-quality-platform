"""Shared fixtures. Fast tests use fakes (token check, Kafka, file store); the rest get a
real Postgres and OPA in throwaway containers."""

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt
import psycopg
import pytest
from cqp.decide import db as decide_db
from cqp.decide.app import create_app as create_decision_app
from cqp.decide.policy import Policy
from cqp.gather.app import create_app
from cqp.notify import db as notify_db
from cqp.store import db as store_db
from fastapi.testclient import TestClient

# What a GitLab CI token says about the job: payments-api's pipeline, on a protected branch.
CLAIMS = {
    "project_path": "payments/payments-api",
    "sha": "9f3c" * 10,
    "job_id": 4412,
    "ref_protected": "true",
    "user_login": "dev",
}
SECRET = "pact-secret"
POLICY_DIR = Path(__file__).parent.parent / "policy"
T0 = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
# Test tokens by name. Each one changes only what its tests need: another repo, a role, a branch.
TOKENS = {
    "good-token": CLAIMS,
    "branch-token": {**CLAIMS, "ref_protected": "false"},
    "other-repo-token": {**CLAIMS, "project_path": "checkout/checkout-web"},
    "approver-token": {**CLAIMS, "cqp_role": "approver", "user_login": "lead"},
    "security-token": {**CLAIMS, "cqp_role": "security", "user_login": "secops"},
    "platform-token": {**CLAIMS, "cqp_role": "platform", "user_login": "platform-oncall"},
    "oncall-token": {**CLAIMS, "cqp_role": "oncall", "user_login": "sre"},
    "retried-job-token": {**CLAIMS, "job_id": 4413},
    "no-sha-token": {k: v for k, v in CLAIMS.items() if k != "sha"},
    "deployer-token": {**CLAIMS, "project_path": "platform/deployer", "sha": "d" * 40},
    "deployer-branch-token": {
        **CLAIMS,
        "project_path": "platform/deployer",
        "ref_protected": "false",
    },
}


def fake_verify(token: str) -> dict:
    # Stands in for TokenVerifier: only the tokens above are valid.
    if token not in TOKENS:
        raise jwt.InvalidTokenError("bad token")
    return TOKENS[token]


# What the ingestion API sent to Kafka and to file storage, for tests to look at.
@pytest.fixture
def published():
    return []


@pytest.fixture
def stored():
    return {}


# Ingestion API with Kafka and file storage faked. Only these two repos own a service.
@pytest.fixture
def client(published, stored):
    # Fakes for Kafka and file storage: they just remember what they were given.
    def publish(topic, key, event):
        published.append((topic, key, event))

    def store(event_id, files):
        stored[event_id] = files
        return f"mem://{event_id}"

    secrets = {"pact-broker": SECRET, "argo-rollouts": SECRET, "runtime": SECRET}
    owns = {("payments/payments-api", "payments-api"), ("checkout/checkout-web", "checkout-web")}
    return TestClient(
        create_app(
            fake_verify,
            publish,
            store,
            secrets,
            owns=lambda project, service: (project, service) in owns,
        )
    )


# Real Postgres and OPA in throwaway containers, shared by the whole run.
# Skipped when Docker isn't available (on Colima, set TESTCONTAINERS_RYUK_DISABLED=true),
# unless CQP_REQUIRE_DOCKER is set, as in CI: there a skipped suite must fail, not look green.


def unavailable(what: str, error: Exception):
    if os.environ.get("CQP_REQUIRE_DOCKER"):
        pytest.fail(f"{what} unavailable and CQP_REQUIRE_DOCKER is set: {error}")
    pytest.skip(f"{what} unavailable: {error}")


@pytest.fixture(scope="session")
def pg_url():
    from testcontainers.community.postgres import PostgresContainer

    try:
        # driver=None gives a plain postgresql:// URL, which psycopg 3 accepts.
        with PostgresContainer("postgres:16-alpine", driver=None) as pg:
            yield pg.get_connection_url()
    except Exception as e:
        unavailable("Postgres container", e)


@pytest.fixture
def conn(pg_url):
    with psycopg.connect(pg_url) as c:
        # Every test starts with empty tables.
        store_db.migrate(c)
        decide_db.migrate(c)
        notify_db.migrate(c)
        c.execute(
            "TRUNCATE signals, builds, decisions, waiting_pipelines, team_policies, waivers, "
            "kill_switches, break_glass, policy_versions"
        )
        c.commit()
        yield c


@pytest.fixture(scope="session")
def opa_url():
    from testcontainers.core.container import DockerContainer

    # Created inside the try too: without Docker even building the container object fails.
    try:
        opa = (
            DockerContainer("openpolicyagent/opa:1.0.0")
            .with_command("run --server --addr 0.0.0.0:8181")
            .with_exposed_ports(8181)
        )
        opa.start()
    except Exception as e:
        unavailable("OPA container", e)
    url = f"http://{opa.get_container_host_ip()}:{opa.get_exposed_port(8181)}"
    # OPA takes a moment to start; wait for /health before handing out the URL.
    for _ in range(50):
        try:
            if httpx.get(f"{url}/health").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    else:
        opa.stop()
        unavailable("OPA container", TimeoutError("never became healthy"))
    yield url
    opa.stop()


# A clock tests can move: set now[0] to jump ahead in time.
@pytest.fixture
def now():
    return [T0]


# The Decision API against the real Postgres and OPA, with Kafka faked.
@pytest.fixture
def setup(conn, pg_url, opa_url, now, published):
    policy = Policy(POLICY_DIR, opa_url)
    policy.install()

    def connect():
        return psycopg.connect(pg_url)

    def publish(topic, key, event):
        published.append((topic, key, event))

    # The clock is passed in, so tests can jump past deadlines without sleeping.
    api = TestClient(
        create_decision_app(connect, policy, fake_verify, publish, clock=lambda: now[0])
    )
    return api, connect, policy
