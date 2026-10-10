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

**Test it in Postman** ([setup](13-postman-testing.md)): folder *07 · Production deploy*, which uses a new throwaway `e2e-svc-pm-…` service each run.

---

[← 6. Pending decisions and resume](06-pending-and-resume.md) · [8. Webhooks →](08-webhooks.md)
