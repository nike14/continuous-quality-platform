# 2. Upload a report

[← All flows](README.md) · Step 2 of 12

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
| `check` | `tests.unit`, `tests.component`, `tests.integration`, `tests.api`, `tests.contract`, `tests.e2e`, `scan.sca`, `scan.container`, `scan.secrets`, `static.quality`, `perf.load`, `build.provenance` | `CQP_CHECK` |
| `format` | `junit`, `sarif`, `cyclonedx`, `k6`, or `json` (only for `build.provenance`) | `CQP_FORMAT` |
| `image` | Image digest, or `null` before the build | `.cqp/image-digest` |
| `files` | The tool's own output | `CQP_FILES` glob |

**CQP:** checks the token (401), the meta (422), ownership (403) and size (413, over 50 MB in
total). It saves the files, puts one event on Kafka and answers. The store worker then parses the
file into a signal (pass/fail plus counts, failing test names, CVE IDs, p95 latency).

**Response:** `202 {"event_id": "3f9a…c21"}`. The same upload sent twice gets the same ID and is
stored once.

**Test it in Postman** ([setup](12-postman-testing.md)): every folder starts with uploads; folder *09 Refused requests* shows the 401, 403 and 422 cases.

---

[← 1. How a CI job calls CQP](01-ci-integration.md) · [3. PR gate: blocked, then fixed →](03-pr-gate.md)
