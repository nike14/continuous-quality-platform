"""Wires the ingestion API to real infrastructure. Run: uv run uvicorn cqp.gather.server:app"""

import json
import os
from pathlib import Path

from cqp.catalog import Catalog
from cqp.gather.app import create_app
from cqp.gather.auth import TokenVerifier
from cqp.kafka import kafka_publisher  # raises if Kafka doesn't ack, so CI retries


def local_store(root: Path):
    """Raw files on local disk; swap for S3 in production."""

    def store(event_id: str, files: list[tuple[str, bytes]]) -> str:
        """Saves one upload's files under its event ID and returns the folder."""
        folder = root / event_id
        folder.mkdir(parents=True, exist_ok=True)
        used: set[str] = set()
        for i, (name, data) in enumerate(files, 1):
            base = Path(name).name  # basename only, no path tricks
            if base in ("", ".", ".."):
                base = f"upload-{i}"  # no usable name: "", "/" or ".."
            if base in used:
                base = f"{i}-{base}"  # a/junit.xml and b/junit.xml must both be kept
            used.add(base)
            (folder / base).write_bytes(data)
        return str(folder)

    return store


app = create_app(
    verify_token=TokenVerifier(os.environ["CQP_JWKS_URL"]),
    publish=kafka_publisher(os.environ.get("CQP_KAFKA", "localhost:9092")),
    store=local_store(Path(os.environ.get("CQP_RAW_DIR", "raw"))),
    webhook_secrets=json.loads(os.environ.get("CQP_WEBHOOK_SECRETS", "{}")),
    owns=Catalog(Path(os.environ.get("CQP_POLICY_DIR", "policy"))).owns,
    max_upload_bytes=int(os.environ.get("CQP_MAX_UPLOAD_MB", "50")) * 1024 * 1024,
)
