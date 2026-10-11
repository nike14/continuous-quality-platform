# 9. Kill switch

[← All flows](README.md) · Step 9 of 14

**Caller:** the platform team (`cqp_role: platform`), when a rule is wrong for everyone (for example
a bad rule release).

```json
POST /v1/kill-switches
{"rule": "baseline.tests.unit", "reason": "bad rule release 1.4", "hours": 1}
```

Response `201` with `kill_id` and `expires_at`, at most 72 hours. Every service's findings for that
rule then only warn: `(rule switched off: bad rule release 1.4)`. `GET /v1/kill-switches` lists
them; `DELETE /v1/kill-switches/{kill_id}` (platform only) switches the rule back on (`204`).

**Test it in Postman** ([setup](13-postman-testing.md)): folder *09 · Kill switch*.

---

[← 8. Webhooks](08-webhooks.md) · [10. When rules are set, and changing them mid-flow →](10-rules-mid-flow.md)
