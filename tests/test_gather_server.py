"""The ingestion API's local file store (the stand-in for S3)."""

import importlib
from pathlib import Path

import cqp.kafka
import pytest

POLICY_DIR = Path(__file__).parent.parent / "policy"


class NoKafka:
    """Importing the server builds a Kafka producer; this one connects to nothing."""

    def __init__(self, config):
        pass


@pytest.fixture
def server(monkeypatch):
    # The module wires itself from env vars at import, so set them first.
    monkeypatch.setenv("CQP_JWKS_URL", "https://gitlab.example/oauth/discovery/keys")
    monkeypatch.setenv("CQP_POLICY_DIR", str(POLICY_DIR))
    monkeypatch.setattr(cqp.kafka, "Producer", NoKafka)
    return importlib.reload(importlib.import_module("cqp.gather.server"))


def test_files_are_saved_under_the_event_id(server, tmp_path):
    ref = server.local_store(tmp_path)("e-1", [("junit.xml", b"<testsuite/>")])
    assert Path(ref) == tmp_path / "e-1"
    assert (tmp_path / "e-1" / "junit.xml").read_bytes() == b"<testsuite/>"


def test_path_parts_in_a_file_name_are_dropped(server, tmp_path):
    # A name like "../../x" must not write outside the event's folder.
    root = tmp_path / "raw"
    ref = server.local_store(root)(
        "e-2", [("../../outside.xml", b"x"), ("reports/junit.xml", b"y")]
    )
    assert sorted(p.name for p in Path(ref).iterdir()) == ["junit.xml", "outside.xml"]
    assert not (tmp_path / "outside.xml").exists()


def test_every_part_is_kept_even_without_a_usable_name(server, tmp_path):
    # Unnamed parts, names that reduce to nothing, and two parts with the same name.
    parts = [
        ("", b"1"),
        ("", b"2"),
        ("/", b"3"),
        ("..", b"4"),
        ("a/junit.xml", b"5"),
        ("b/junit.xml", b"6"),
    ]
    ref = Path(server.local_store(tmp_path)("e-3", parts))
    saved = {p.name: p.read_bytes() for p in ref.iterdir()}
    assert sorted(saved.values()) == [b"1", b"2", b"3", b"4", b"5", b"6"]
    assert all(p.parent == ref for p in ref.iterdir())  # nothing written outside the folder
