# 5. Team policy

[← All flows](README.md) · Step 5 of 13

**Caller:** the `quality-policy` job, only when `.quality/policy.yaml` changes.

| Pipeline | Command | Request | Needs |
| --- | --- | --- | --- |
| MR | `cqp policy check` | `PUT /v1/team-policies/{service}?dry_run=true` | Token from the owning repo |
| Default branch | `cqp policy publish` | `PUT /v1/team-policies/{service}?dry_run=false` | Owning repo and `ref_protected: true` |

The body is the YAML file as JSON:

```json
{
  "extra_checks": {"integration": {"tests.e2e": {"deadline": 45, "on_missing": "warn"}}},
  "on_fail": {"tests.e2e": {"integration": "block"}, "static.quality": {"build": "validate"}},
  "thresholds": {"perf.load": {"max": 150, "effect": "validate"}}
}
```

**Response (200):**

```json
{"version": "1b2c3d4e5f6a",
 "backtest": {"decisions": 40, "changed": 1,
              "examples": [{"decision_id": "…", "stage": "integration",
                            "was": "PROCEED", "would_be": "REQUIRE_VALIDATION"}]}}
```

A dry run publishes nothing. Unknown keys, or loosening a locked baseline cell (security scans,
contract tests at integration, secrets), give a `422` with the reasons. `GET
/v1/team-policies/{service}` returns the current version and policy, no token needed.

Every later decision for the service uses it and records its version. In the seeded example (`scripts/seed.sh`),
checkout-web's load test reports p95 212 ms against the team's 150 ms limit, so the integration
gate answers `REQUIRE_VALIDATION` from `team.perf.load.threshold`.

A policy published while a change is in the middle of its pipeline applies from that change's next
gate request, on the evidence already uploaded. See
[10. When rules are set, and changing them mid-flow](10-rules-mid-flow.md) for a worked example.

## More examples

Each file in `examples/team-policies/` is a complete `.quality/policy.yaml` with comments.
`tests/test_examples.py` checks that they're still accepted, or still rejected for the reasons
they list.

| File | What the team wants |
| --- | --- |
| `strict-payments.yaml` | Catch problems at the MR: contract, API, SCA and quality failures block the PR, and the PR waits for them |
| `wait-for-more-checks.yaml` | Wait for component, container, API, e2e and contract results the baseline doesn't ask for |
| `performance-budget.yaml` | A p95 latency limit that blocks, also checked at build |
| `judge-at-more-stages.yaml` | Judge checks at stages where the baseline ignores them (integration tests at build) |
| `softer-where-allowed.yaml` | Loosen cells that aren't locked (flaky e2e tests only warn) |
| `flaky-quarantine.yaml` | Quarantine two flaky e2e tests under a ticket; p95 at most 10% worse than the protected branch |
| `rejected/loosens-locked-rules.yaml` | **422:** loosens secrets, contract and SCA cells |
| `rejected/typos.yaml` | **422:** misspelt key, unknown stage, effect and check |
| `rejected/quarantine-out-of-bounds.yaml` | **422:** quarantines a contract test, and too many e2e tests |

Rules for writing one:

- **Stricter is always allowed.** Making an effect stricter (`warn` < `validate` < `block`), or
  judging a check at a stage the baseline doesn't, is always accepted.
- **Looser only where it isn't locked.** The locked cells are in `locked` in
  `policy/baseline.rego`: unit and integration tests at prod-deploy, contract tests at integration
  and prod-deploy, secrets scans everywhere, and SCA and container scans from build on. For a
  one-off, use a waiver instead.
- **`on_fail` judges, `extra_checks` waits.** An `on_fail` cell applies only when the check
  reports. Add it to `extra_checks` if the gate should also wait for it (see `strict-payments.yaml`).
- **The baseline's expectations win.** An `extra_checks` entry for a check the baseline already
  expects at that stage is ignored. You can't shorten its deadline or soften its `on_missing`.
- **Thresholds need a value.** They compare `payload.value`, which today only `perf.load` (k6 p95,
  ms) has. They apply at every stage where that evidence is present. `max` is absolute;
  `max_increase_pct` compares with the protected branch's last value; set either or both.
- **Quarantine is bounded.** Test checks only (never contract tests or scans), at most 10 tests per
  check, each under a ticket. It only softens a failure whose failing tests are all listed.

To see a policy's effect without publishing it, run `cqp policy check`. To see exactly how OPA
reads a team policy, see [OPA examples](../opa-examples.md) 04, 05, 06 and 14.

---

[← 4. Build gate: waiver and break-glass](04-build-gate.md) · [6. Pending decisions and resume →](06-pending-and-resume.md)
