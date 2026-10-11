# 7. Production deploy

[← All flows](README.md) · Step 7 of 13

**Callers:** monitoring, the incident tool and release management (webhooks); the **deploy
system's** pipeline (`cqp gate --stage prod-deploy`); Argo Rollouts after the canary.

Only the deploy system may ask about production: a project listed in `policy/trust.yaml`
(`platform/deployer`) with `ref_protected: true`. A service's own pipeline gets a `403`, so it
can't deploy around its own gates. The request must name the image (`422` without it):

```json
POST /v1/decisions                    (token of platform/deployer)
{"service": "payments-api", "stage": "prod-deploy", "image": "sha256:…", "fix_ticket": null}
```

CQP judges the image's own evidence (through `build.provenance`, results on its commit count too)
plus the service's production state:

| Production state | Sent by | Effect |
| --- | --- | --- |
| Error budget used up | Monitoring (`type: slo`) | `REQUIRE_VALIDATION`, unless sent as a fix |
| Active incident | Incident tool (`type: incident`) | `REQUIRE_VALIDATION`, unless sent as a fix |
| Released as a fix (`fix_ticket`, `cqp gate --fix INC-77`) | The deploy pipeline | Warning `baseline.fix_release` |
| A canary failed in the last 24h | Argo Rollouts | Warning `baseline.recent_rollback` |
| Organisation-wide freeze | Release management (`type: freeze`) | `BLOCKED`, even for fixes |
| No SLO update in the last 5 minutes | (nothing arrived) | Tier-1: `REQUIRE_VALIDATION`; others: warning |

If CQP can't be reached at `prod-deploy`, the gate asks for approval (exit 2) instead of carrying on.

A `REQUIRE_VALIDATION` from CQP itself is approved with `cqp approve --decision <id> --reason ...`
by an approver of the service who didn't start the deploy ([flow 13](13-approvals-flaky-preview.md)).

**Which evidence counts.** The image's evidence is what its trusted build says: the image→commit
link is recorded only from a protected branch. A result about the image uploaded from an
unprotected branch, on another commit, can only make the decision stricter: its failure counts,
its pass is ignored. Findings already on the protected branch are *not* relaxed here, unlike
earlier stages: production judges the image as it is.

---

[← 6. Pending decisions and resume](06-pending-and-resume.md) · [8. Webhooks →](08-webhooks.md)
