"""Evidence store and store worker against a real Postgres (the `conn` fixture in conftest)."""

import json

import pytest
from cqp.store.db import latest, save
from cqp.store.worker import DLQ_TOPIC, poll_once, process


def signal(event_id, check="tests.unit", status="pass", image=None, at="2026-10-07T10:00:00Z"):
    return {
        "event_id": event_id,
        "service": "payments-api",
        "commit": "9f3c",
        "image": image,
        "check": check,
        "kind": "outcome",
        "status": status,
        "run": event_id,
        "producer": "ci:payments",
        "payload": {"failed": 0},
        "raw_ref": None,
        "observed_at": at,
    }


def test_same_event_is_stored_once(conn):
    save(conn, signal("e-1"))
    save(conn, signal("e-1"))
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 1


def test_latest_attempt_wins_and_commit_results_reach_the_image(conn):
    save(conn, signal("build", "build.provenance", image="sha256:ab12"))
    save(
        conn, signal("try-1", status="fail", at="2026-10-07T10:00:00Z")
    )  # unit tests ran on the commit
    save(conn, signal("try-2", status="pass", at="2026-10-07T10:05:00Z"))  # rerun
    save(conn, signal("scan", "scan.sca", image="sha256:ab12"))

    result = latest(conn, "payments-api", "sha256:ab12")

    assert result["tests.unit"]["event_id"] == "try-2"
    assert set(result) == {"build.provenance", "tests.unit", "scan.sca"}


# Minimal stand-ins for a Kafka message and consumer: only what process() uses.
class Msg:
    def __init__(self, event):
        self._value = json.dumps(event).encode()

    def value(self):
        return self._value

    def key(self):
        return b"payments-api:9f3c"


class Consumer:
    def __init__(self, *messages):
        self.committed = []
        self.messages = list(messages)

    def poll(self, timeout):
        return self.messages.pop(0) if self.messages else None

    def commit(self, message, asynchronous):
        self.committed.append(message)


RAW = {
    "event_id": "e-9",
    "service": "payments-api",
    "commit": "9f3c",
    "image": None,
    "check": "tests.contract",
    "format": "pact",
    "run": "v/9",
    "producer": "adapter:pact-broker",
    "payload": {"status": "pass", "consumer": "web"},
    "raw_ref": None,
    "received_at": "2026-10-07T10:00:00Z",
}


def test_message_is_written_then_committed(conn):
    consumer, parked = Consumer(), []
    process(Msg(RAW), conn, consumer, lambda *a: parked.append(a))

    assert conn.execute("SELECT status FROM signals WHERE event_id = 'e-9'").fetchone()[0] == "pass"
    assert len(consumer.committed) == 1 and parked == []


def test_bad_event_goes_to_dlq_and_is_not_stored(conn):
    consumer, parked = Consumer(), []
    process(Msg({**RAW, "format": "mystery"}), conn, consumer, lambda *a: parked.append(a))

    assert parked[0][0] == DLQ_TOPIC
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 0
    assert len(consumer.committed) == 1  # parked, so we move on


def test_a_dlq_write_that_fails_is_not_committed(conn):
    # Regression: the offset was committed even when Kafka never took the DLQ event.
    def kafka_down(*args):
        raise RuntimeError("Kafka did not acknowledge the event")

    consumer = Consumer()
    with pytest.raises(RuntimeError):
        process(Msg({**RAW, "format": "mystery"}), conn, consumer, kafka_down)
    assert consumer.committed == []  # so the message is read again after the restart


class MsgOk(Msg):
    def error(self):
        return None


class ErrorMsg(Msg):
    def error(self):
        return "broker gone"


def test_poll_once_stores_a_message_and_skips_an_empty_poll(conn):
    consumer = Consumer(MsgOk(RAW))
    poll_once(consumer, conn, lambda *a: None)
    poll_once(consumer, conn, lambda *a: None)  # nothing left: returns quietly
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 1
    assert len(consumer.committed) == 1


def test_poll_once_raises_on_a_kafka_error(conn):
    with pytest.raises(RuntimeError, match="broker gone"):
        poll_once(Consumer(ErrorMsg(RAW)), conn, lambda *a: None)


def test_any_failure_in_the_newest_run_wins(conn):
    # One job reported tests.unit twice (two modules); the later, passing one mustn't hide
    # the failure.
    failing = signal("module-a", status="fail", at="2026-10-07T10:00:00Z")
    passing = signal("module-b", status="pass", at="2026-10-07T10:01:00Z")
    save(conn, {**failing, "run": "job-7"})
    save(conn, {**passing, "run": "job-7"})
    assert latest(conn, "payments-api", None, commit="9f3c")["tests.unit"]["status"] == "fail"
