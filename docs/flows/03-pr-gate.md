# 3. PR gate: blocked, then fixed

[← All flows](README.md) · Step 3 of 13

**Callers:** the MR pipeline's report jobs, then its gate job (`cqp gate --stage pr`).

1. `tests.unit` uploaded with one failing test, `scan.secrets` uploaded clean (see [Upload a report](02-upload-report.md)).
2. The gate asks:

   ```json
   POST /v1/decisions
   {
     "service": "payments-api",
     "stage": "pr",
     "image": null,
     "event_ids": ["3f9a…c21", "8b7e…04d"],
     "merge_request": 7,
     "fix_ticket": null
   }
   ```

3. **CQP:** reads the commit from the token, loads the newest result per check for it, checks that
   every check the PR stage expects (`policy/expectations.yaml`: `tests.unit`, `scan.secrets`) has
   reported, runs the rules in OPA (`policy/baseline.rego`), saves the decision and publishes it to
   the notifier.
4. **Response (200):**

   ```json
   {
     "decision_id": "a41c…9e",
     "service": "payments-api",
     "subject": "<commit sha>",
     "stage": "pr",
     "status": "final",
     "outcome": "BLOCKED",
     "findings": [
       {"rule": "baseline.tests.unit", "effect": "block",
        "message": "tests.unit failed: PaymentRefundTest.partial",
        "items": ["PaymentRefundTest.partial"]}
     ],
     "waiting": [],
     "policy_version": "…",
     "mode": "enforce",
     "input": { "...": "the exact evidence, waivers and kill switches it was decided on" }
   }
   ```

   `outcome` is what the pipeline acts on, `findings` says why (rule, effect, message, items), and
   `status: final` means nothing was still missing.
5. The developer fixes the test; the retried job uploads a passing report for the **same commit**
   with a new job ID. The newer run wins, so the next gate request returns `PROCEED`.

`GET /v1/decisions/{decision_id}` returns a stored decision with its full `input`: the audit trail.

**Test it in Postman** ([setup](13-postman-testing.md)): folder *03 · PR gate*.

---

[← 2. Upload a report](02-upload-report.md) · [4. Build gate: waiver and break-glass →](04-build-gate.md)
