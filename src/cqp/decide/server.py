"""Wires the Decision API to Postgres and the OPA sidecar.

Run: uv run uvicorn cqp.decide.server:app
"""

import os
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

from cqp.decide import db as decide_db
from cqp.decide.app import create_app
from cqp.decide.policy import Policy
from cqp.gather.auth import TokenVerifier
from cqp.kafka import kafka_publisher
from cqp.store import db as store_db

database_url = os.environ["CQP_DATABASE_URL"]
# Both schemas: decisions read the evidence tables, and whichever service starts first creates them.
with psycopg.connect(database_url) as conn:
    store_db.migrate(conn)
    decide_db.migrate(conn)

policy = Policy(
    Path(os.environ.get("CQP_POLICY_DIR", "policy")),
    os.environ.get("CQP_OPA_URL", "http://localhost:8181"),
)
policy.install()

pool = ConnectionPool(database_url)
# Decision events must never slow a gate down, so they don't wait for Kafka's acknowledgement.
app = create_app(
    pool.connection,
    policy,
    TokenVerifier(os.environ["CQP_JWKS_URL"]),
    kafka_publisher(os.environ.get("CQP_KAFKA", "localhost:9092"), wait_for_ack=False),
)
