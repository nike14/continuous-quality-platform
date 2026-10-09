"""The Kafka publisher's two modes, against a broker that never answers."""

import cqp.kafka
import pytest


class UnreachableKafka:
    """A producer whose broker is down: nothing is ever acknowledged."""

    def __init__(self, config):
        self.flushed = False

    def produce(self, topic, key, value):
        pass

    def poll(self, timeout):
        pass

    def flush(self, timeout):
        self.flushed = True
        return 1  # one message still undelivered


@pytest.fixture(autouse=True)
def kafka_down(monkeypatch):
    monkeypatch.setattr(cqp.kafka, "Producer", UnreachableKafka)


def test_ingestion_publish_fails_when_kafka_does_not_acknowledge():
    with pytest.raises(RuntimeError):
        cqp.kafka.kafka_publisher("kafka:9092")("topic", "key", {})


def test_decision_publish_never_waits_on_kafka():
    # Regression: waiting 5s for Kafka made gates time out and fall back during an outage.
    cqp.kafka.kafka_publisher("kafka:9092", wait_for_ack=False)("topic", "key", {})
