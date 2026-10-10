# 11. Read-only views

[← All flows](README.md) · Step 11 of 12

No token needed.

| Request | Returns |
| --- | --- |
| `GET /portal` | HTML page: active waivers, rules switched off, recent break-glass, each service's tier, owner and team policy version, recent decisions |
| `GET /v1/decisions/{id}` | One decision with its input |
| `GET /v1/waivers?service=…` | Active waivers |
| `GET /v1/kill-switches` | Rules switched off |
| `GET /v1/team-policies/{service}` | Current team policy and version |
| `GET /metrics` (ingestion, through the gateway) | Prometheus metrics |

---

**Test it in Postman** ([setup](12-postman-testing.md)): folder *08 Look around*.

---

[← 10. What CQP calls: GitLab and Slack](10-notifications.md) · [12. Testing with Postman →](12-postman-testing.md)
