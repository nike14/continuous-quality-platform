# 10. When rules are set, and changing them mid-flow

[← All flows](README.md) · Step 10 of 13

**Rules are applied when the gate asks, not when evidence is uploaded.** An upload only stores facts
about a change (passed or failed, counts, failing test names, CVE IDs, p95 latency). Every
`POST /v1/decisions` applies whatever rules are active **at that moment**.

So rules can be set before a change starts, or changed while it is in the middle of its pipeline:

- **Set before the flow:** every decision for the change uses them from the first gate.
- **Changed mid-flow:** the **next** decision uses the new rules, on the evidence already stored.
  Nothing needs to be uploaded again.
- **Decisions already made never change.** They are insert-only and keep the rules they were made
  with. Asking again creates a new decision.

## Which rules, who sets them, and when they take effect

| Rule | Who sets it | How | Takes effect | Recorded on the decision as |
| --- | --- | --- | --- | --- |
| Baseline rules (`policy/baseline.rego`) | Platform team | MR to this repo, then redeploy | When the Decision API starts with the new files | `policy_version` |
| Expected checks and deadlines (`policy/expectations.yaml`) | Platform team | Same | Same | `policy_version` |
| Service catalog: tier, mode, owning repo (`policy/catalog.yaml`) | Platform team | Same | When the Decision API (tier, mode, ownership) and the ingestion API (upload ownership) restart | `input.tier`, `mode` |
| Deployers (`policy/trust.yaml`) | Platform team | Same | When the Decision API restarts | (who may ask for `prod-deploy`) |
| Team policy | The service's team | `cqp policy publish` from the default branch ([5](05-team-policy.md)) | The next decision for that service | `input.team_policy_version` |
| Waiver | Approvers, security | `cqp waiver` or the portal ([4](04-build-gate.md)) | The next decision for that service, until it expires or is revoked | `input.waivers` |
| Kill switch | Platform team | `cqp kill-switch` ([9](09-kill-switch.md)) | The next decision, for every service | `input.kill_switches` |
| Break-glass | On-call, approvers, security | `cqp break-glass` ([4](04-build-gate.md)) | The next decision for that change and stage (re-run the gate job) | `input.break_glass` |
| Production state | Monitoring, incident tool, release management | Webhooks ([8](08-webhooks.md)) | The next `prod-deploy` decision | `input.runtime` |

Two groups behave differently:

- **Files in `policy/`** are read **once, when the Decision API starts**, and are part of the image
  (`Dockerfile` copies `policy/`). Changing them is a code change: MR, review, deploy. Locally:
  `docker compose up -d --build decision-api ingestion-api`.
- **Everything else** is read from the database **on every decision**, so it applies from the next
  gate request, with no deploy.

Every decision stores the exact input it was made on (team policy version, waivers, kill switches,
break-glass, production state) and the `policy_version` (a hash of `baseline.rego` and
`expectations.yaml`). That's how an old decision can still be explained, and replayed on its own
rules, after the rules change.

## Example: a team adds a latency limit in the middle of a change

A service's integration tests pass and its load test reports p95 **212 ms**. The baseline has no
latency limit, so the gate proceeds. Then the team publishes a limit of 150 ms, and later accepts
the slow result for a few days with a waiver. The evidence is uploaded once; only the rules change.

```
uploads ──► ask ──► publish team policy ──► ask ──────────────► waiver ──► ask
            PROCEED   (p95 max 150 ms)       REQUIRE_VALIDATION            PROCEED_WITH_WARNING
            decision A                       decision B                    decision C
            (A stays PROCEED forever)
```

The outputs below are from a real run against the local stack, on a throwaway service
(`e2e-svc-rules-…`, owned by `platform/e2e` in `policy/catalog.yaml`), so it doesn't change any real
service's rules. IDs are shortened.

### 1. Upload the evidence (no team policy yet)

`POST /v1/reports` twice for the same commit and image (see [2](02-upload-report.md)):
`tests.integration` with `junit-pass.xml`, and `perf.load` with `k6-summary.json` (p95 212.4 ms).

```json
{"event_id": "ff46…04c1"}
{"event_id": "4b6e…3a92"}
```

### 2. Ask the integration gate

```json
POST /v1/decisions
{"service": "e2e-svc-rules-24555", "stage": "integration", "image": "sha256:rules-…"}
```

```json
{"decision_id": "6d95…333b", "status": "final", "outcome": "PROCEED", "findings": [],
 "input": {"team_policy_version": null, "...": "..."}}
```

No rule says anything about latency yet, so this is decision **A: PROCEED**.

### 3. The team publishes a latency limit, mid-flow

```json
PUT /v1/team-policies/e2e-svc-rules-24555          (token from the owning repo, protected branch)
{"thresholds": {"perf.load": {"max": 150, "effect": "validate"}}}
```

```json
{"version": "9413c240fa70",
 "backtest": {"decisions": 1, "changed": 1,
              "examples": [{"decision_id": "6d95…333b", "stage": "integration",
                            "was": "PROCEED", "would_be": "REQUIRE_VALIDATION"}]}}
```

The backtest already tells the team what the new rule does: the decision just made would now need
validation. On an MR, `cqp policy check` shows this before anything is published.

### 4. Ask again: same evidence, new rule

Exactly the same request as step 2. Nothing was uploaded again.

```json
{"decision_id": "6dd6…f9bf", "status": "final", "outcome": "REQUIRE_VALIDATION",
 "findings": [{"rule": "team.perf.load.threshold", "effect": "validate",
               "message": "perf.load is 212.4 ms, over the team's limit of 150"}],
 "input": {"team_policy_version": "9413c240fa70", "...": "..."}}
```

Decision **B** used the team policy, and records its version.

### 5. The first decision hasn't changed

```
GET /v1/decisions/6d95…333b
```

```json
{"decision_id": "6d95…333b", "outcome": "PROCEED", "input": {"team_policy_version": null, "...": "..."}}
```

Decision A still says `PROCEED`, made with no team policy. Rules never rewrite history.

### 6. An approver waives the finding, mid-flow

```json
POST /v1/waivers                                    (token with cqp_role: approver, owning repo)
{"service": "e2e-svc-rules-24555", "rule": "team.perf.load.threshold",
 "reason": "tuning the cache this sprint", "ticket": "PERF-12", "days": 3}
```

```json
{"waiver_id": "66fa…80fe", "expires_at": "2026-10-13T13:45:57+00:00", "...": "..."}
```

### 7. Ask again

```json
{"decision_id": "91f5…68cc", "status": "final", "outcome": "PROCEED_WITH_WARNING",
 "findings": [{"rule": "team.perf.load.threshold", "effect": "warn",
               "message": "perf.load is 212.4 ms, over the team's limit of 150 (waived until 2026-10-13T13:45:57+00:00, PERF-12)"}]}
```

Decision **C**: the finding still shows, but only warns. When the waiver expires in 3 days, the next
decision needs validation again.

All three decisions have the same `policy_version`, because the baseline files didn't change; what
changed is recorded in each decision's `input`.

### Try it yourself

From a terminal:

```bash
U=http://localhost:8080
SVC=e2e-svc-rules-$RANDOM SHA=$(python3 -c "import secrets; print(secrets.token_hex(20))") IMG="sha256:rules-$RANDOM"
tok() { uv run -q python scripts/dev_token.py "$SHA" "${1:-}" platform/e2e; }   # [role]
up()  { curl -s $U/v1/reports -H "Authorization: Bearer $(tok)" \
          -F "meta={\"service\":\"$SVC\",\"check\":\"$1\",\"format\":\"$2\",\"image\":\"$IMG\"}" -F "files=@examples/reports/$3"; echo; }
ask() { curl -s $U/v1/decisions -H "Authorization: Bearer $(tok)" -H 'Content-Type: application/json' \
          -d "{\"service\":\"$SVC\",\"stage\":\"integration\",\"image\":\"$IMG\"}" | jq '{decision_id, outcome, findings}'; }

up tests.integration junit junit-pass.xml; up perf.load k6 k6-summary.json; sleep 2
ask                                                          # PROCEED
curl -s -X PUT $U/v1/team-policies/$SVC -H "Authorization: Bearer $(tok)" -H 'Content-Type: application/json' \
  -d '{"thresholds":{"perf.load":{"max":150,"effect":"validate"}}}' | jq .backtest
ask                                                          # REQUIRE_VALIDATION
curl -s $U/v1/waivers -H "Authorization: Bearer $(tok approver)" -H 'Content-Type: application/json' \
  -d "{\"service\":\"$SVC\",\"rule\":\"team.perf.load.threshold\",\"reason\":\"tuning the cache\",\"ticket\":\"PERF-12\",\"days\":3}" | jq .waiver_id
ask                                                          # PROCEED_WITH_WARNING
```

## Changing a baseline file mid-flow

A change to `policy/` works the same way, with a deploy in the middle:

1. A change gets a decision under the current rules, for example `policy_version: 3d3d35dc537a`.
2. The platform team merges a change to `policy/expectations.yaml` (say `perf.load` at integration
   goes from `on_missing: warn` to `validate`) and deploys. Locally:
   `docker compose up -d --build decision-api ingestion-api`.
3. The Decision API pushes the new `baseline.rego` into OPA on start and stores it in
   `policy_versions`. The next decision for the same change uses the new rules and carries a new
   `policy_version`.
4. Older decisions keep their old `policy_version`. `replay()` re-runs them on the Rego stored for
   that version, so they can still be explained.

To see the effect before deploying, check the rules in the MR with the tests (`tests/test_policy.py`)
and look at the `TooManyBlocks` alert after release; a bad rule release is what the
[kill switch](09-kill-switch.md) is for.

## What happens to a change that is already in progress

| Situation | What the next decision does |
| --- | --- |
| A final decision was made under the old rules | Unchanged. Asking again makes a new decision under the new rules |
| A decision is **pending** (waiting for evidence) | The gate's next poll is judged under the new rules. A pending answer is only reused while the rules are the same (`policy_version` unchanged), so a deploy gives a new decision |
| A team adds an expected check (`extra_checks`) while a change is waiting | The new check is waited for too, with its deadline counted from the **first** time this change was asked about at this stage |
| A deadline in `expectations.yaml` changes | The new deadline applies, counted from the same first-asked time. A check can become missing straight away if the new deadline has already passed |
| A waiver, kill switch or break-glass expires | The next decision no longer applies it. Nothing is re-evaluated in the background |
| A service moves from `advisory` to `enforce` | The outcome is worked out the same way; the gate starts acting on it after the Decision API restarts with the new catalog |

Rules are only ever applied when someone asks. CQP doesn't re-run old decisions when rules change; a
pipeline sees the new rules the next time its gate job runs. To apply them to a change whose gate
has already passed, re-run that gate job.

---

[← 9. Kill switch](09-kill-switch.md) · [11. What CQP calls: GitLab and Slack →](11-notifications.md)
