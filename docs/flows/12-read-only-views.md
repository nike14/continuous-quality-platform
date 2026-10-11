# 12. Read-only views

[← All flows](README.md) · Step 12 of 13

No token needed, except for a full decision record: it carries the evidence the decision was
made on (failing tests, CVE IDs), so it needs any valid CQP token. The portal shows outcomes only.

| Request | Returns |
| --- | --- |
| `GET /portal` | HTML page: active waivers, rules switched off, recent break-glass and approvals, flaky test candidates, each service's tier, owner and team policy version, recent decision outcomes |
| `GET /v1/decisions/{id}` (token) | One decision with its input; 401 without a token |
| `GET /v1/flaky?service=…&days=14` | Tests that failed and then passed on the same commit |
| `GET /v1/waivers?service=…` | Active waivers |
| `GET /v1/kill-switches` | Rules switched off |
| `GET /v1/team-policies/{service}` | Current team policy and version |
| `GET /metrics` (ingestion, through the gateway) | Prometheus metrics |

---

[← 11. What CQP calls: GitLab and Slack](11-notifications.md) · [13. Approvals, flaky tests and checking locally →](13-approvals-flaky-preview.md)
