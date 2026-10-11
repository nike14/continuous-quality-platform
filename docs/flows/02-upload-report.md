# 2. Upload a report

[← All flows](README.md) · Step 2 of 13

**Caller:** any CI job extending `.quality-report`, via `cqp report`.

```
POST /v1/reports
Authorization: Bearer <CI job token>
Content-Type: multipart/form-data

meta  = {"service": "payments-api", "check": "tests.unit", "format": "junit", "image": null}
files = junit.xml            (one or more files; repeat the "files" part)
```

| Field | Values | Comes from |
| --- | --- | --- |
| `service` | A service in `policy/catalog.yaml` owned by the token's repo | `--service`, default `CI_PROJECT_NAME` |
| `check` | `tests.unit`, `tests.component`, `tests.integration`, `tests.api`, `tests.contract`, `tests.e2e`, `contract.schema`, `db.migration`, `scan.sca`, `scan.container`, `scan.secrets`, `static.quality`, `perf.load`, `build.provenance` | `CQP_CHECK` |
| `format` | `junit`, `sarif`, `cyclonedx`, `k6`, `schema-compat` (schema-registry answers, one `<subject>.json` each), `squawk` (migration lint), or `json` (only for `build.provenance`) | `CQP_FORMAT` |
| `image` | Image digest, or `null` before the build | `.cqp/image-digest` |
| `files` | The tool's own output | `CQP_FILES` glob |

**CQP:** checks the token (401), the meta (422), ownership (403) and size (413, over 50 MB in
total). It saves the files, puts one event on Kafka and answers. The store worker then parses the
file into a signal (pass/fail plus counts, failing test names, CVE IDs, p95 latency).

**Response:** `202 {"event_id": "3f9a…c21"}`. The same upload sent twice gets the same ID and is
stored once.

## Where the uploaded file is kept: S3 in production, local disk today

The original file never goes on Kafka or into Postgres. The ingestion API saves it to **object
storage** and puts only a pointer to it, `raw_ref`, in the Kafka event. The store worker reads the
file back through `raw_ref`, parses it, and saves the summary (counts, failing names, CVE IDs) in
Postgres.

| | Production (the target) | This prototype (today) |
| --- | --- | --- |
| Where files go | An **S3 bucket**, one folder per `event_id` | A Docker volume, `raw`, mounted at `/data/raw` (`CQP_RAW_DIR`) in both the ingestion API and the store worker |
| `raw_ref` looks like | `s3://<bucket>/<event_id>/` | `/data/raw/<event_id>` |
| Saved by | An S3 version of `local_store()` | `local_store()` in `src/cqp/gather/server.py` |
| Read by | An S3 version of `read_local()` | `read_local()` in `src/cqp/store/worker.py` |
| Old files | A lifecycle rule expires them | Kept until `docker compose down -v` |

**Moving to S3 is not built yet.** It means replacing those two functions; nothing else changes,
because the rest of CQP only passes `raw_ref` around. Local disk works here only because both
containers share one volume on one machine; in production the ingestion API and the store worker
run separately, so they need shared storage like S3.

Why keep the original file at all, and why not in Kafka or Postgres: see *Raw report files (object
storage)* in [WALKTHROUGH.md](../WALKTHROUGH.md#raw-report-files-object-storage).

---

[← 1. How a CI job calls CQP](01-ci-integration.md) · [3. PR gate: blocked, then fixed →](03-pr-gate.md)
