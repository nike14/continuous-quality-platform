"""Ingestion API: the one front door for evidence.

Verify the sender, keep the raw file, publish, acknowledge.
"""

import hashlib
import json
from collections.abc import Callable
from typing import Annotated

import jwt
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import ValidationError

from cqp.gather.adapters import ADAPTERS, valid_signature
from cqp.gather.event import RAW_TOPIC, ReportMeta, raw_event
from cqp.metrics import INGESTED, metrics_response

Publish = Callable[[str, str, dict], None]  # (topic, key, event)
Store = Callable[[str, list[tuple[str, bytes]]], str]  # (event_id, files) -> raw_ref


def digest(files: list[tuple[str, bytes]]) -> str:
    """A hash of an upload's names and bytes: the same upload twice gives the same value."""
    h = hashlib.sha256()
    for name, data in files:
        h.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


def create_app(
    verify_token: Callable[[str], dict],
    publish: Publish,
    store: Store,
    webhook_secrets: dict[str, str],
    owns: Callable[[str, str], bool],
    max_upload_bytes: int = 50 * 1024 * 1024,
) -> FastAPI:
    """Builds the ingestion API.

    Token check, Kafka, storage and ownership are passed in, so tests can swap them.
    """
    app = FastAPI(title="CQP ingestion")

    def emit(event: dict) -> str:
        """Publishes a raw event to Kafka and returns its ID."""
        # Key by service + commit so one change's events stay ordered on one partition.
        publish(RAW_TOPIC, f"{event['service']}:{event['commit']}", event)
        return event["event_id"]

    def reject(source: str, result: str, status: int, detail: str):
        """Counts a rejected request and returns the HTTP error to raise."""
        INGESTED.labels(source, result).inc()
        return HTTPException(status, detail)

    @app.get("/metrics")
    def metrics():
        """Prometheus scrape endpoint."""
        return metrics_response()

    @app.post("/v1/reports", status_code=202)
    async def upload_report(
        meta: Annotated[str, Form()],
        files: Annotated[list[UploadFile], File()],
        authorization: Annotated[str, Header()],
    ):
        """POST /v1/reports: a CI job's report files, plus what they are."""
        try:
            claims = verify_token(authorization.removeprefix("Bearer "))
        except jwt.PyJWTError as e:
            raise reject("ci", "rejected_auth", 401, "invalid CI token") from e
        try:
            m = ReportMeta.model_validate_json(meta)
        except ValidationError as e:
            raise reject("ci", "rejected_invalid", 422, str(e)) from e
        # Otherwise any CI job could report a pass for someone else's service.
        if not owns(claims["project_path"], m.service):
            raise reject(
                "ci", "rejected_owner", 403, f"{claims['project_path']} doesn't own {m.service}"
            )

        saved, total = [], 0
        for f in files:
            # Read at most one byte past the limit, so an oversized upload is never held whole.
            data = await f.read(max_upload_bytes - total + 1)
            total += len(data)
            if total > max_upload_bytes:
                limit_mb = max_upload_bytes // (1024 * 1024)
                raise reject(
                    "ci", "rejected_too_large", 413, f"reports over {limit_mb} MB in total"
                )
            saved.append((f.filename or "", data))  # the store names unnamed parts
        # Identity comes from the verified token, never from what the job sent.
        event = raw_event(
            producer=f"ci:{claims['project_path']}",
            service=m.service,
            check=m.check,
            fmt=m.format,
            repo=claims["project_path"],
            commit=claims["sha"],
            run=str(claims["job_id"]),
            image=m.image,
            content=digest(saved),
        )
        # Files first: the event only points at them, and the store worker reads them back.
        event["raw_ref"] = store(event["event_id"], saved)
        event_id = emit(event)
        INGESTED.labels("ci", "accepted").inc()
        return {"event_id": event_id}

    @app.post("/v1/hooks/{tool}", status_code=202)
    async def webhook(tool: str, request: Request):
        """POST /v1/hooks/{tool}: a signed result from a tool.

        For example Pact Broker or Argo Rollouts.
        """
        if tool not in ADAPTERS:
            raise reject("unknown", "rejected_invalid", 404, "unknown tool")
        body = await request.body()  # the signature is over the raw bytes, so check before parsing
        secret = webhook_secrets.get(tool)
        if not secret or not valid_signature(secret, body, request.headers.get("X-CQP-Signature")):
            raise reject(tool, "rejected_auth", 401, "bad signature")
        try:
            events = ADAPTERS[tool](json.loads(body))
        except (ValueError, KeyError, TypeError) as e:
            raise reject(tool, "rejected_invalid", 422, "unexpected payload") from e
        event_ids = [emit(e) for e in events]
        INGESTED.labels(tool, "accepted").inc(len(event_ids))
        return {"event_ids": event_ids}

    return app
