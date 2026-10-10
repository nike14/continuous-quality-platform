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

Every later decision for the service uses it and records its version. In the Postman example,
checkout-web's load test reports p95 212 ms against the team's 150 ms limit, so the integration
gate answers `REQUIRE_VALIDATION` from `team.perf.load.threshold`.

A policy published while a change is in the middle of its pipeline applies from that change's next
gate request, on the evidence already uploaded. See
[10. When rules are set, and changing them mid-flow](10-rules-mid-flow.md) for a worked example.

**Test it in Postman** ([setup](13-postman-testing.md)): folder *05 · Team policy*.

---

[← 4. Build gate: waiver and break-glass](04-build-gate.md) · [6. Pending decisions and resume →](06-pending-and-resume.md)
