# 6. Pending decisions and resume

[← All flows](README.md) · Step 6 of 13

**Callers:** the gate job, then the notifier (which calls GitLab).

CQP never decides on half the evidence. If an expected check hasn't reported and its deadline hasn't
passed, or the caller's own uploads aren't stored yet, the answer is:

```json
{
  "status": "pending",
  "outcome": "REQUIRE_VALIDATION",
  "findings": [],
  "waiting": [{"check": "perf.load", "deadline": "2026-10-10T14:20:40+00:00"}]
}
```

1. `cqp gate` asks again every 5 seconds, up to `--max-wait` (120s by default). Each repeat from the
   same job, waiting on the same things, gets the same decision back.
2. If still pending, the gate exits 2 and the runner is freed.
3. The notifier remembers the gate job. Every 10 seconds it checks the stored evidence; when the
   checks arrive or the deadline passes, it calls GitLab's
   `POST /projects/:id/jobs/:job_id/retry`. The re-run gate job gets the final decision.

**Test it in Postman** ([setup](13-postman-testing.md)): folder *06 · Pending decision*. Watch `docker compose logs -f notifier` for the retry.

---

[← 5. Team policy](05-team-policy.md) · [7. Production deploy →](07-production-deploy.md)
