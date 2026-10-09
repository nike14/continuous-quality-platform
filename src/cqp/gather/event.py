"""The raw event every gathered result becomes before it goes on Kafka."""

import hashlib
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

RAW_TOPIC = "cqp.signals.raw"

# The checks CQP knows about. A new kind of check starts here.
Check = Literal[
    "tests.unit",
    "tests.component",
    "tests.integration",
    "tests.api",
    "tests.contract",
    "tests.e2e",
    "scan.sca",
    "scan.container",
    "scan.secrets",
    "static.quality",
    "perf.load",
    "build.provenance",
]
Format = Literal["junit", "sarif", "cyclonedx", "pact", "k6", "json"]


class ReportMeta(BaseModel):
    """What the CI step says about an upload. Repo, commit and run come from the token instead."""

    service: str
    check: Check
    format: Format
    image: str | None = None


def event_id(
    producer: str, check: str, service: str, commit: str, run: str, content: str = ""
) -> str:
    """A stable ID for one result from one run."""
    # Same upload sent twice gets the same ID, so it is stored once downstream.
    # A retried CI job has a new job ID (run), so its result counts as new evidence.
    # content (a hash of the files) keeps two different reports from one job apart.
    key = f"{producer}|{check}|{service}|{commit}|{run}|{content}"
    return hashlib.sha256(key.encode()).hexdigest()


def raw_event(
    *,
    producer: str,
    service: str,
    check: str,
    fmt: str,
    commit: str,
    run: str,
    repo: str | None = None,
    image: str | None = None,
    payload: dict | None = None,
    content: str = "",
) -> dict:
    """Builds the event that goes on the raw Kafka topic."""
    return {
        "event_id": event_id(producer, check, service, commit, run, content),
        "producer": producer,
        "service": service,
        "check": check,
        "format": fmt,
        # None for webhooks: tools outside CI don't tell us a repo.
        "repo": repo,
        "commit": commit,
        "run": run,
        "image": image,
        "payload": payload,
        "raw_ref": None,  # set by the ingestion API once the files are saved
        "received_at": datetime.now(UTC).isoformat(),  # "newest result wins" uses this
    }
