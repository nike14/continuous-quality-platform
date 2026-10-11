# 8. Webhooks

[← All flows](README.md) · Step 8 of 13

**Callers:** tools outside CI, each with a webhook template configured on the tool's side.

```
POST /v1/hooks/{tool}
Content-Type: application/json
X-CQP-Signature: sha256=<HMAC-SHA256 of the raw body, keyed with the tool's secret>
```

Secrets come from `CQP_WEBHOOK_SECRETS` (locally `local-secret` for every tool). A tool without a
secret, or a body that doesn't match the signature, gets `401`; an unknown tool `404`; a body of the
wrong shape `422`.

| Tool | Body | Becomes |
| --- | --- | --- |
| `pact-broker` | `{"provider": "checkout-web", "provider_version": "<commit>", "consumer": "mobile-app", "verification_url": "…", "success": false}` | `tests.contract` for that provider and commit |
| `argo-rollouts` | `{"service": "…", "commit": "…", "image": "sha256:…", "analysis_run": "…", "phase": "Failed"}` | `deploy.canary`; only `Successful` passes |
| `runtime` | `{"type": "slo", "service": "…", "error_budget_remaining": 0.42, "at": "…"}` | `runtime.slo` |
| `runtime` | `{"type": "incident", "service": "…", "id": "INC-1", "severity": "sev1", "state": "open"}` | `runtime.incident` (send `"state": "resolved"` to close it) |
| `runtime` | `{"type": "freeze", "active": true, "at": "…"}` | `org.freeze` for every service |

Any body may also carry `observed_at` (ISO 8601 with a timezone), so a late delivery can't
overwrite newer state. **Response:** `202 {"event_ids": ["…"]}`.

---

[← 7. Production deploy](07-production-deploy.md) · [9. Kill switch →](09-kill-switch.md)
