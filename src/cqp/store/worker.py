"""Consumes cqp.signals.raw, normalizes each event and writes it to Postgres."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from prometheus_client import start_http_server

from cqp.gather.event import RAW_TOPIC
from cqp.metrics import EVIDENCE_DELAY, STORED
from cqp.store.db import migrate, save
from cqp.store.normalize import BadEventError, normalize

DLQ_TOPIC = "cqp.signals.dlq"


def read_local(raw_ref: str) -> list[tuple[str, bytes]]:
    """Reads files saved by the ingestion API's local store; swap for S3 in production."""
    return [(p.name, p.read_bytes()) for p in sorted(Path(raw_ref).iterdir())]


def process(msg, conn, consumer, dlq, read_raw=read_local) -> None:
    """Handles one Kafka message: store it, or park it in the DLQ."""
    event = json.loads(msg.value())
    try:
        signal = normalize(event, read_raw)
    except BadEventError as e:
        # Parked, not lost; counts as missing evidence (never as a pass) until fixed and replayed.
        dlq(DLQ_TOPIC, msg.key(), {"event": event, "error": str(e)})
        STORED.labels("dlq").inc()
    else:
        with conn.transaction():
            save(conn, signal)
        STORED.labels("stored").inc()
        EVIDENCE_DELAY.observe(
            (datetime.now(UTC) - datetime.fromisoformat(event["received_at"])).total_seconds()
        )
    # Commit the offset only after the write: a crash before this re-reads the message,
    # and the idempotent insert makes that safe.
    consumer.commit(message=msg, asynchronous=False)


def main() -> None:
    """Reads raw events from Kafka until stopped."""
    from confluent_kafka import Consumer, Producer

    bootstrap = os.environ.get("CQP_KAFKA", "localhost:9092")
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": "evidence-writer",
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    producer = Producer({"bootstrap.servers": bootstrap, "enable.idempotence": True})

    def dlq(topic, key, value):
        """Sends an unreadable event to the dead-letter topic."""
        producer.produce(topic, key=key, value=json.dumps(value).encode())
        # Wait for it: the offset is committed next, and an unsent DLQ event would be lost.
        producer.flush(5)

    start_http_server(9100)  # /metrics for Prometheus
    with psycopg.connect(os.environ["CQP_DATABASE_URL"]) as conn:
        migrate(conn)
        consumer.subscribe([RAW_TOPIC])
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                raise RuntimeError(msg.error())  # let the orchestrator restart us
            process(msg, conn, consumer, dlq)
