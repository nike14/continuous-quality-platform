"""Kafka publishing shared by the CQP services."""

import json

from confluent_kafka import Producer


def kafka_publisher(bootstrap: str, wait_for_ack: bool = True):
    """Returns a publish(topic, key, event) function."""
    # Idempotence: if the client retries a send, Kafka still keeps one copy.
    producer = Producer({"bootstrap.servers": bootstrap, "enable.idempotence": True})

    def publish(topic: str, key: str | bytes | None, event: dict) -> None:
        """Sends one event, keyed so related events stay in order."""
        producer.produce(topic, key=key, value=json.dumps(event).encode())
        if not wait_for_ack:
            # Buffered and retried by the client in the background; the caller never waits on Kafka.
            producer.poll(0)
            return
        # Only report success once Kafka has the event. flush() returns how many are still queued.
        if producer.flush(5):
            raise RuntimeError("Kafka did not acknowledge the event")

    return publish
