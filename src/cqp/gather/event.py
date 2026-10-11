"""The raw event every gathered result becomes before it goes on Kafka."""

import hashlib
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, model_validator

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
# Formats a CI job may upload: each has a parser in the store worker. Pact results come from the
# Pact Broker webhook instead. "json" is only for build provenance, which needs no parsing.
Format = Literal["junit", "sarif", "cyclonedx", "k6", "json"]


class ReportMeta(BaseModel):
    """What the CI step says about an upload. Repo, commit and run come from the token instead."""

    service: str
    check: Check
    format: Format
    image: str | None = None

    @model_validator(mode="after")
    def json_only_for_provenance(self) -> "ReportMeta":
        """Refused here with a 422, rather than accepted and parked in the DLQ later."""
        if self.format == "json" and self.check != "build.provenance":
            raise ValueError("format json is only for build.provenance")
        return self


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
    observed_at: str | None = None,
    trusted: bool = True,
) -> dict:
    """Builds the event that goes on the raw Kafka topic.

    observed_at is when the result was produced, if the sender knows; otherwise when it arrived.
    trusted: a signed webhook, or a CI job on a protected branch (see store.db.latest).
    """
    received_at = datetime.now(UTC).isoformat()
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
        # "Newest result wins" orders by observed_at, so a late upload of an older run can't
        # replace a newer one. received_at measures how long evidence takes to arrive.
        "observed_at": observed_at or received_at,
        "received_at": received_at,
        "trusted": trusted,
    }
