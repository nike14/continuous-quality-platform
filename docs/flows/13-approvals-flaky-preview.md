# 13. Approvals, flaky tests and checking locally

[← All flows](README.md) · Step 13 of 13

Three ways CQP helps a person instead of only stopping them: someone can **approve** what needs a
look, **flaky** tests are named instead of hidden by a rerun, and a developer can **preview** a
gate before pushing.

## Approving a decision that needs validation

**Callers:** an approver of the service, with `cqp approve` (in production, from the portal
behind SSO).

```bash
cqp approve --decision 3f2a… --reason "p95 spike is the cold cache after the migration" --ticket PERF-3
```

`POST /v1/decisions/{id}/approvals` with `{"reason": "...", "ticket": "PERF-3"}`:

| Check | Refused with |
| --- | --- |
| Role is `approver` or `security` (from the token) | 403 `only approvers can approve a decision` |
| The approver's repo owns the service (security: any service) | 403 `… doesn't own …` |
| The decision is final and `REQUIRE_VALIDATION` (a block stays a block; pending waits for evidence) | 409 |
| The approver isn't who started the job that asked (`requested_by`) | 403 `you can't approve a decision you asked for` |

The approval records exactly the `validate` findings of that decision (rule and message). Re-run
the gate job: those findings now only warn, `… (approved by lead: …)`, for 24 hours. A new or
different finding (a worse load test, another missing check) isn't covered and needs a new look.
Approvals are in the decision input, so a replay sees them.

## Flaky tests

- **Passed only on a rerun.** When the newest run of a check passed but an earlier run of the same
  change failed, the decision gets a warning: `tests.unit passed only on a rerun: 1 earlier run(s)
  of this change failed (RefundTest.partial); if it's flaky, quarantine it`. A rerun that made a
  failure go away shows in the MR comment instead of disappearing.
- **Candidates.** `GET /v1/flaky?service=payments-api&days=14` (and the portal) lists tests that
  failed and then passed on the same commit, with how many commits they did it on.
- **Quarantine.** The team adds the test to `.quality/policy.yaml`, reviewed in its MR with the
  usual backtest:

  ```yaml
  quarantine:
    tests.e2e: {ticket: QA-12, tests: [CheckoutFlow.applePay]}
  ```

  A failing check whose failing tests are all quarantined only warns, `(quarantined under QA-12)`.
  One more failing test and it blocks again. Only test checks can be quarantined (never contract
  tests or scans), at most 10 tests per check, and always under a ticket.

## Checking locally before pushing

**Callers:** a developer on a laptop, or a pre-push hook. Needs `CQP_URL`, no token.

```bash
cqp preview --service payments-api --stage pr --report "tests.unit:junit:build/test-results/**/*.xml"
```

The CLI reads the reports with the same parsers the store worker uses, and sends
`POST /v1/preview` with `{"service", "stage", "evidence": {check: {kind, status, payload}}}`. CQP
judges them with the real baseline, the team's policy and the active waivers and kill switches,
and records nothing. The answer has the `outcome`, the `findings`, and `not_checked`: expected
checks the preview didn't see, which CI will run. The command exits like the gate. Production
can't be previewed (422): it depends on the image and live production state.

`cqp explain <decision>` (needs a token) prints any decision's findings, each with what you can do
next: fix, rerun, ask for a waiver, approve, or quarantine.

---

[← 12. Read-only views](12-read-only-views.md) · [All flows](README.md)
