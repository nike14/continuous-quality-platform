# OPA examples: what the rules answer for a given input

`policy/baseline.rego` runs in an OPA sidecar next to the Decision API. For every decision, CQP
builds one JSON **input** (evidence, missing checks, team policy, waivers, kill switches,
break-glass and, for production deploys, production state). It asks OPA for
`cqp/baseline/findings` and gets back a list of **findings**. CQP then picks the outcome, and the
strictest effect wins:

| Strictest effect | Outcome | `cqp gate` exits |
| --- | --- | --- |
| none | `PROCEED` | 0 |
| `warn` | `PROCEED_WITH_WARNING` | 0 |
| `validate` | `REQUIRE_VALIDATION` | 2 |
| `block` | `BLOCKED` | 1 |

`examples/opa/` has 16 inputs, each with what OPA answers for it. Each file is:

```json
{"about": "what it shows", "rule": "findings", "input": {...}, "expect": [...]}
```

`tests/test_examples.py` runs them against a real OPA, so they stay true when the Rego changes.

## Run them

```bash
./scripts/opa-examples.sh            # every example: prints the input's story, OPA's answer, OK
./scripts/opa-examples.sh 07 08      # only these
```

The script starts a throwaway OPA container and loads the Rego into it, the same way the Decision
API does at startup. To use an OPA that's already running, set `OPA_URL`.

To try your own input by hand:

```bash
docker run -d --rm --name opa -p 8181:8181 openpolicyagent/opa:1.0.0 run --server --addr 0.0.0.0:8181
curl -X PUT --data-binary @policy/baseline.rego localhost:8181/v1/policies/cqp-baseline
jq '{input}' examples/opa/01-failing-unit-tests-block-a-pr.json \
  | curl -s localhost:8181/v1/data/cqp/baseline/findings -d @- | jq
docker stop opa
```

Any rule can be asked by name, for example `cqp/baseline/on_fail` for the whole stage matrix or
`cqp/baseline/waiver_errors` for a waiver request. A real decision's full input is in the
decision itself (`GET /v1/decisions/{id}`, field `input`). You can paste it as-is.

## The examples

| File | Stage | Shows | Outcome |
| --- | --- | --- | --- |
| `01-failing-unit-tests-block-a-pr` | pr | A failing check gets its stage's effect from `on_fail` | BLOCKED |
| `02-same-failure-depends-on-stage` | pr | Contract tests only warn at the PR; at build they block | PROCEED_WITH_WARNING |
| `03-missing-and-stale-evidence` | build | Missing and too-old results use `on_missing` | BLOCKED |
| `04-team-threshold` | integration | A team's p95 limit (212 ms against 150) | REQUIRE_VALIDATION |
| `05-team-makes-a-rule-stricter` | integration | A team turns e2e failures from validate into block | BLOCKED |
| `06-team-cant-loosen-a-locked-rule` | pr | A locked cell keeps the baseline's effect | BLOCKED |
| `07-waiver-accepts-the-named-cve` | build | A waiver naming the CVE turns block into warn | PROCEED_WITH_WARNING |
| `08-new-cve-is-not-covered` | build | One more CVE and the same waiver no longer covers it | BLOCKED |
| `09-kill-switch` | build | A rule switched off only warns | PROCEED_WITH_WARNING |
| `10-break-glass` | build | Everything warns, plus a break-glass finding | PROCEED_WITH_WARNING |
| `11-prod-deploy-unhealthy-production` | prod-deploy | Error budget, incident and recent rollback | REQUIRE_VALIDATION |
| `12-prod-deploy-fix-release` | prod-deploy | A fix gets past the error budget and incident | PROCEED_WITH_WARNING |
| `13-freeze-and-stale-health` | prod-deploy | A freeze can't be waived; stale health on tier-1 | BLOCKED |
| `14-team-policy-errors` | | Why a team policy file is refused (`cqp policy check`) | 422 |
| `15-waiver-errors` | | Why a waiver request is refused | 403 |
| `16-kill-switch-errors` | | Why a kill switch request is refused | 403 |

A few in full (inputs trimmed to the parts that matter):

### 01: failing unit tests at the PR

```json
{"stage": "pr",
 "evidence": {"tests.unit": {"kind": "outcome", "status": "fail",
                             "payload": {"failed": 1, "failing": ["PaymentRefundTest.partial"]}}},
 "missing": [], "team": {}, "waivers": [], "kill_switches": [], "break_glass": null}
```

```json
[{"rule": "baseline.tests.unit", "effect": "block",
  "message": "tests.unit failed: PaymentRefundTest.partial",
  "items": ["PaymentRefundTest.partial"]}]
```

`on_fail["tests.unit"]["pr"]` is `block`. `items` lists what failed, which is what a waiver can
name.

### 04: a team threshold

The team's policy is part of the input, so the same Rego serves every team:

```json
{"stage": "integration",
 "evidence": {"perf.load": {"kind": "measurement", "status": "pass",
                            "payload": {"metric": "p95_ms", "value": 212, "unit": "ms"}}},
 "team": {"thresholds": {"perf.load": {"max": 150, "effect": "validate"}}}}
```

```json
[{"rule": "team.perf.load.threshold", "effect": "validate",
  "message": "perf.load is 212 ms, over the team's limit of 150"}]
```

The load test itself passed. Only the team's limit makes it a finding.

### 07 and 08: a waiver covers named items only

```json
"waivers": [{"rule": "baseline.scan.sca", "items": ["CVE-2026-1234"], "ticket": "SEC-42",
             "expires_at": "2026-10-20T00:00:00+00:00"}]
```

When the scan fails on `["CVE-2026-1234"]` (07), the finding is kept but only warns:

```json
[{"rule": "baseline.scan.sca", "effect": "warn", "waiver": "SEC-42",
  "message": "scan.sca failed: CVE-2026-1234 (waived until 2026-10-20T00:00:00+00:00, SEC-42)",
  "items": ["CVE-2026-1234"]}]
```

When it fails on `["CVE-2026-1234", "CVE-2026-9999"]` (08), the waiver doesn't cover everything
that failed, so it blocks again with no waiver mentioned. A new critical CVE is never hidden by an
old waiver.

### 10: break-glass

```json
"break_glass": {"by": "oncall-amy", "reason": "payments down", "ticket": "INC-301"}
```

```json
[{"rule": "baseline.break_glass", "effect": "warn",
  "message": "break-glass by oncall-amy: payments down (INC-301)"},
 {"rule": "baseline.evidence_required", "effect": "warn",
  "message": "scan.sca did not report before its deadline (overridden by break-glass)"},
 {"rule": "baseline.tests.unit", "effect": "warn",
  "message": "tests.unit failed: PaymentRefundTest.partial (overridden by break-glass)",
  "items": ["PaymentRefundTest.partial"]}]
```

Nothing blocks, but nothing disappears either: the MR comment and Slack show what was overridden.

### 14: checking a team policy file

Ask the `team_policy_errors` rule instead of `findings`:

```json
{"team": {"on_fail": {"scan.secrets": {"pr": "warn"},
                      "tests.contract": {"integration": "validate"},
                      "tests.e2e": {"integration": "warn"}}}}
```

```json
["scan.secrets at pr can't be looser than the organisation baseline (block)",
 "tests.contract at integration can't be looser than the organisation baseline (block)"]
```

`tests.e2e` at integration isn't locked, so a team may soften it and it isn't listed.

## Adding an example

1. Write `examples/opa/NN-name.json` with `about`, `rule`, `input` and `"expect": []`.
2. Run `./scripts/opa-examples.sh NN`, check that the answer is what you meant, and paste it into
   `expect`. Findings are sorted by `rule`, and error lists are sorted.
3. `uv run pytest tests/test_examples.py`.

See also [team policy examples](flows/05-team-policy.md#more-examples) and
[the walkthrough's rules section](WALKTHROUGH.md#step-6-the-rules-themselves).
