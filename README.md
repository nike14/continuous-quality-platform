# CQP: gather, store, decide, notify

A working end-to-end slice of the Continuous Quality Platform (CQP):

1. **Gather:** take the results tools already produce, check who sent them, keep the raw file and
   put a small event on Kafka.
2. **Store:** read those events, turn each into one standard signal and save it in Postgres.
3. **Decide:** when a pipeline asks, check the expected evidence is complete, apply the rules
   with OPA, record the decision and return one of four outcomes.
4. **Notify:** tell people and pipelines. This means one MR comment, a commit status, Slack for
   production problems, and re-running a gate job that was waiting for evidence.
5. **Production quality:** canary results and production state (error budget, incidents, change
   freeze) feed the next production decision.
6. **Observability:** every service exposes Prometheus metrics, and alert rules page or open a
   ticket when CQP itself is unhealthy.

## Run everything locally

Needs Docker and [uv](https://docs.astral.sh/uv/).

```bash
uv run python scripts/dev_token.py > /dev/null   # once: creates a local signing key in dev/
docker compose up -d --build                     # Kafka, Postgres, OPA and all four services
./scripts/demo.sh                                # upload a failing test report, ask the PR gate
```

The demo uploads a JUnit report with one failing test, then asks the PR gate. Expected result:

```
cqp: BLOCKED at pr · policy … · decision …
cqp:   [warn] scan.secrets did not report before its deadline
cqp:   [block] tests.unit failed: PaymentRefundTest.partial
gate exit code: 1
```

The notifier then logs the MR comment and commit status it would post. With no GitLab
configured, it logs instead of calling GitLab.

| Service | What it runs | Reach it at |
| --- | --- | --- |
| `gateway` | Caddy: decisions, policies, waivers, kill switches and `/portal` → Decision API; everything else → ingestion | http://localhost:8080 (`CQP_URL`), portal at `/portal` |
| `ingestion-api` | Gather | via gateway |
| `store-worker` | Store | logs only |
| `decision-api` | Decide (loads `policy/` into OPA on start) | via gateway |
| `notifier` | Notify | logs only |
| `kafka`, `kafka-init` | Kafka, plus creating the three topics | localhost:9094 |
| `postgres` | Evidence and decisions | `postgresql://cqp:cqp@localhost:5433/cqp` |
| `opa` | Policy engine sidecar | internal |
| `dev-idp` | Serves the dev **public** key, standing in for GitLab's OIDC keys | internal |
| `prometheus` | Scrapes all four services; evaluates the alert rules | http://localhost:9090 (alerts at `/alerts`) |

Fill every table with varied data to look at (for example in TablePlus at `127.0.0.1:5433`,
user, password and database all `cqp`). It covers 3 services, every report format, and all four
outcomes at every stage, plus one gate still waiting for evidence, a published team policy
(checkout-web), waivers (one active, one revoked), a break-glass and a kill switch:

```bash
./scripts/seed.sh    # about a minute; goes through the real APIs, safe to run again
```

End-to-end checks against the running stack (about 10 minutes, mostly the outage scenarios):

```bash
./scripts/e2e.sh     # 22 checks: gate outcomes, trust, dead-letter, resume, outages; exit code = failures
```

It stops and restarts the Decision API and Kafka on purpose to check how CQP behaves when
they're down.

Useful commands:

```bash
docker compose logs -f store-worker notifier       # watch evidence and notifications flow
psql postgresql://cqp:cqp@localhost:5433/cqp -c "select check_name, status from signals"
docker compose down -v                             # stop and delete all data
```

`scripts/dev_token.py` prints a token with the same claims GitLab puts in a CI job's
`id_token`. The private key stays in `dev/private` and is never served. It's for local use only.

## How gathering works

Evidence arrives three ways, and all of it goes through one Ingestion API.

```
CI job ──(cqp report)──┐
                       ├──► Ingestion API ──► raw file to storage
Tool webhook ──────────┘      │ verify sender
(Pact Broker, scanners,       │ validate
 deploy system, alerts)       └──► Kafka topic cqp.signals.raw
```

| Path | How it works | Status |
| --- | --- | --- |
| **CI jobs** | The golden-path template runs `cqp report` after each job. It uploads the files the job already produced (JUnit, SARIF, Pact, k6). Teams don't change their tests. | Built |
| **Tools outside CI** | The tool sends a signed webhook to `/v1/hooks/<tool>`. One small adapter per tool turns its payload into events. | Built for Pact Broker |
| **Production** | The deploy system and monitoring send events through the same webhook path. | Next: add adapters |

### Trust

- **CI uploads:** each job gets a short-lived GitLab OIDC token (`id_tokens`, audience `cqp`).
  Repo, commit and job ID are read from the **verified token**, never from the upload, so a job
  can't report results for someone else's change.
- **Service ownership:** a repo may only report for, gate on and set policy for the services
  `policy/catalog.yaml` says it owns (`repo:`). Anything else gets a 403, so `payments-api`'s
  pipeline can't report a pass for `checkout-web`. Services missing from the catalog are refused too.
- **Production:** only the deploy system may ask for `prod-deploy` decisions: a project listed in
  `policy/trust.yaml`, with a token from a protected branch, naming the image it deploys. A
  service's own pipeline can't, so it can't deploy around its own gates. The deploy system's
  commit says nothing about the image, so its evidence comes through the image and the commit
  that built it.
- **Refused means stop:** when CQP refuses a gate request (bad token, wrong repo), the gate fails
  the job and says why. Only an unreachable CQP uses the fallback (continue, or approval in
  production). Otherwise a misconfigured pipeline would pass quietly.
- **Webhooks:** each tool signs its body with a shared secret (`X-CQP-Signature: sha256=…`).
  A tool with no configured secret is rejected.

### What gets published

Every result becomes one raw event on `cqp.signals.raw`:

- `event_id` = hash of producer, check, service, commit and run. **The same upload sent twice gets
  the same ID**, so downstream stores it once.
- Kafka key = `service:commit`, so one change's events stay in order on one partition.
- The raw file goes to storage and the event only carries a reference to it.

### When things fail

| Failure | What happens |
| --- | --- |
| CQP or Kafka down | `cqp report` retries (2s, 4s, 8s), then gives up **without failing the job**. Reports stay as CI artifacts for a later backfill. |
| Kafka slow to acknowledge | The API returns an error instead of "accepted", so CI retries. |
| Bad request (unknown check, bad token) | Rejected with 4xx and not retried. |
| No report files | Nothing sent; a warning in the job log. |

## How storing works

```
Kafka cqp.signals.raw ──► store worker ──► normalize ──► Postgres (signals, builds)
                                │
                                └── unreadable event ──► Kafka cqp.signals.dlq
```

**Normalize.** Each format becomes one standard signal with a `kind`, a `status` and a small payload:

| Format | Kind | What is kept |
| --- | --- | --- |
| JUnit | outcome | total, failed, skipped, names of failing tests |
| SARIF | findings | counts by level, a fingerprint per finding |
| CycloneDX | findings | counts by severity, vulnerability IDs |
| k6 | measurement | p95 latency in ms |
| Pact (webhook) | outcome | the adapter's pass/fail as is |
| Build provenance | outcome | the image digest |

Percentages are never stored: "96% passed" stays as counts, so later policy can see *which* tests
failed. Fingerprints let policy tell a new finding from an old one. Pass/fail against a baseline
(for example, latency) is left to the Decision API.

**Save.**

- `signals` table: one row per event. Inserting the same `event_id` again does nothing, so a
  retried upload or a re-read Kafka message is harmless.
- `builds` table: the build's provenance signal records which image came from which commit. Results
  recorded against the commit (unit tests run before the build) therefore count for the image.
- `latest(service, image)` returns the newest result per check, so a rerun replaces the earlier
  attempt.

**Kafka handling.**

- The worker commits the Kafka offset **only after** the database write. A crash in between means
  the message is read again, and the idempotent insert makes that safe.
- Events that can never be processed (unknown format, unreadable report) go to `cqp.signals.dlq`.
  They are not stored, so they count as **missing** evidence, never as a pass.
- Database or Kafka errors crash the worker on purpose. The orchestrator restarts it and it resumes
  from the last committed offset.

## How deciding works

```
cqp gate ──► Decision API ──► Postgres: latest evidence, first time asked
                 │
                 ├── expectations.yaml: which checks this stage and tier expect
                 ├── complete? no  ──► "pending" + what we're waiting for
                 └── complete? yes ──► OPA (baseline.rego) ──► findings ──► outcome
                                         │
                                         └── decision saved (insert-only) with its input
```

A request names the service, stage (`pr`, `build`, `integration`, `prod-deploy`), the image if
there is one, and the event IDs this pipeline uploaded. The commit comes from the caller's token.

1. **Expected evidence.** `policy/expectations.yaml` lists the checks each stage expects, per tier
   (tier from `policy/catalog.yaml`, a stand-in for the service catalog). Each check has a
   `deadline` in minutes and an `on_missing` effect.
2. **Completeness.** A check that hasn't reported is **waiting** until its deadline, counted from
   the first time this change was asked about at this stage. After that it is **missing**. If the
   caller's own uploads aren't stored yet, CQP waits for them for up to 60 seconds.
3. **Never decide on half the evidence.** While anything is waiting, the answer is
   `status: pending` (outcome `REQUIRE_VALIDATION`) with the list of what it's waiting for.
4. **Rules.** `policy/baseline.rego` runs in an OPA sidecar. A failing check gets the effect
   the stage matrix gives it (for example, failing unit tests block a PR). A missing check gets its
   `on_missing` effect. Each finding has a rule name and a plain message.
5. **Outcome.** The strictest finding wins: `PROCEED`, `PROCEED_WITH_WARNING`,
   `REQUIRE_VALIDATION` or `BLOCKED`. There is no score.
6. **Record and replay.** Every decision, pending or final, is inserted and never updated. It stores
   the exact policy input and a policy version (a hash of the Rego and the expectations), so
   `replay()` reproduces the outcome later.

**`cqp gate`** (the CI step) sends every job's event IDs from `.cqp/events/` and polls every 5s
while the answer is pending, up to `--max-wait` (120s by default).

| Exit code | Meaning |
| --- | --- |
| 0 | Proceed, or proceed with warning |
| 1 | Blocked |
| 2 | Needs validation: an approval or more evidence, or still pending after `--max-wait` |

If CQP can't be reached, the gate continues with a warning before production and asks for
approval (exit 2) at `prod-deploy`.

## How notifying works

```
Decision API ──► Kafka cqp.decisions ──► notifier ──► GitLab: MR comment, commit status
                                            │      └─► Slack (production problems only)
                                            └─► waiting gate jobs ──(sweep every 10s)──► GitLab: retry job
```

The Decision API publishes every decision with its routing context. Project, pipeline, job and
commit come from the caller's token, so a caller can only cause comments or retries in its own
project. The MR number comes from the gate. If Kafka is down the decision is still returned;
notifications may lag, decisions may not.

| What | When | How |
| --- | --- | --- |
| **MR comment** | The gate ran in an MR pipeline | One comment per MR, found by a hidden marker and updated in place. It shows the outcome, each finding with its rule, and what it's waiting for. |
| **Commit status** | Always, when there's a commit | `cqp/<stage>`: success for proceed or warning, failed for blocked, pending while waiting or needing validation |
| **Slack** | Final `prod-deploy` decisions that aren't a clean proceed | One line with the reason and decision ID |
| **Resume** | A decision was pending | See below |

**How resume works.**

- For a pending decision, the notifier remembers the gate job and the checks it's waiting for.
- Every 10s a sweep looks at the stored evidence. When those checks have arrived, or the deadline
  has passed, it asks GitLab to **retry that gate job**. The job asks again and gets the final
  decision.
- CQP never drives the pipeline; GitLab re-runs its own job.
- A final decision for the same change and stage clears the waiting entry.

**Failures.**

- GitLab 4xx (MR closed, job gone): logged and skipped.
- GitLab 5xx: crashes the worker on purpose. The worker commits its Kafka offset only after the
  side effects, so the message is handled again. Comments and statuses are idempotent; a Slack
  message could repeat.

## How production quality works

Testing before a deploy is only part of the picture. Production signals arrive through the same
signed webhook path as everything else, and are stored as append-only signals:

| Webhook | Sent by | Becomes |
| --- | --- | --- |
| `/v1/hooks/argo-rollouts` | Argo Rollouts notification template, after canary analysis | `deploy.canary` for that image (pass or fail) |
| `/v1/hooks/runtime` `{"type": "slo", ...}` | Monitoring, on SLO burn updates | `runtime.slo` with `error_budget_remaining` |
| `/v1/hooks/runtime` `{"type": "incident", ...}` | Incident tool, on open or resolve | `runtime.incident` |
| `/v1/hooks/runtime` `{"type": "freeze", ...}` | Release management | `org.freeze` (service `*`, organisation-wide) |

**Verify.** The rollout controller runs canary analysis and **rolls back by itself**. CQP only
records the result: a central service in the rollback path would add a failure point at the
worst moment.

**Shape the next decision.** Every `prod-deploy` decision reads the service's production state
and adds it to the policy input, so a replay sees exactly the same state. `baseline.rego` then
applies:

| Production state | Effect | Rule |
| --- | --- | --- |
| Error budget used up | validate, unless `cqp gate --fix` | `baseline.error_budget` |
| Active incident | validate, unless `cqp gate --fix` | `baseline.active_incident` |
| A canary failed in the last 24h | warn: use the slowest canary steps | `baseline.recent_rollback` |
| Organisation-wide freeze | block (even fixes; emergencies use break-glass) | `baseline.change_freeze` |
| Health missing or older than 5 min | validate for tier-1, warn otherwise | `baseline.production_health` |

Health is "fresh" if the newest SLO update arrived within 5 minutes. Services with no SLO data
are treated as stale, so tier-1 production deploys need an approval until their monitoring
reports in.

## How team policies and waivers work

The baseline is the organisation's floor. Teams adjust it for their own service in two ways.

### Team policy files

A team keeps `.quality/policy.yaml` in its repo (see `examples/team-policy.yaml`):

```yaml
extra_checks:            # wait for more checks before deciding
  integration:
    tests.e2e: {deadline: 45, on_missing: warn}
on_fail:                 # effect of a failing check, per stage
  tests.e2e: {integration: block}
thresholds:              # limits on measurements
  perf.load: {max: 150, effect: validate}   # p95 ms
```

- **Bounds.** A team can add checks and tighten rules. It can't loosen a locked baseline cell
  (security scans, contract tests at integration, secrets). `baseline.rego` checks this and gives
  a reason: `scan.sca at build can't be looser than the organisation baseline (block)`.
  Unknown keys are rejected too, so a typo doesn't silently do nothing.
- **Review.** On an MR, the `quality-policy` job runs `cqp policy check`: it validates the file and
  backtests it on the service's last 40 decisions ("1 of the last 40 decisions would change:
  integration PROCEED -> BLOCKED"). The reviewer sees the effect before merging.
- **Publish.** On the default branch the same job runs `cqp policy publish`. Only the repo listed
  as the service's `repo` in `catalog.yaml` may publish, and only from a protected branch (both
  read from the CI token). Each version is kept, and every decision records the
  `team_policy_version` it used, so replay still works.
- The baseline's expectations always win over a team's `extra_checks` for the same check.

### Waivers

A waiver accepts a known finding for a while: "this CVE has no fix yet, ticket SEC-118".

```bash
cqp waiver --service ledger-service --rule baseline.scan.sca \
  --reason "no fixed version yet" --ticket SEC-118 --days 7
```

- A waived finding still shows in the decision and MR comment, but only warns:
  `scan.sca failed: {...} (waived until 2026-10-16, SEC-118)`.
- Rules, also in `baseline.rego`:
  - only approvers can create waivers;
  - security rules (SCA, secrets, contracts) need a security approver;
  - at most 30 days, and they expire on their own;
  - missing evidence and a change freeze can't be waived (those need break-glass).
- Active waivers are part of the decision input, so a replay sees exactly the waivers that applied.
- `GET /v1/waivers?service=` lists active ones; `DELETE /v1/waivers/{id}` revokes one.
- Locally the role comes from a `cqp_role` claim (`dev_token.py <sha> approver`). In production
  it would come from a group lookup, and waivers would be created from the portal, not a CI token.

## Kill switches, break-glass and the portal

Two escape hatches, each logged, counted and alerted on so they get reviewed.

| | Kill switch | Break-glass |
| --- | --- | --- |
| **For** | A rule that is wrong (e.g. a bad rule release blocks everyone) | An emergency change that must ship now |
| **Scope** | One rule, every service | One change, one stage |
| **Who** | Platform team (`cqp_role: platform`) | On-call, approvers, security |
| **Lasts** | Up to 72 hours, or until cleared | 24 hours |
| **Effect** | That rule's findings only warn: `(rule switched off: bad release 1.4)` | Every finding only warns, a `baseline.break_glass` finding is added, and nothing waits for slow evidence |
| **Command** | `cqp kill-switch --rule baseline.tests.unit --reason "..." --hours 4` | `cqp break-glass --decision <id> --reason "..." --ticket INC-77`, then re-run the gate job |
| **Alert** | `RuleSwitchedOffTooLong` (ticket, after 4h) | `BreakGlassUsed` (ticket) and a 🚨 Slack message at any stage |

Both are part of the decision input, so a replay of a decision sees the same switches and the
same break-glass. Findings are never hidden: the MR comment still lists what was wrong.

The `TooManyBlocks` alert's runbook points to the kill switch: if one rule jumped after a policy
release, switch it off, fix it, release again.

**Waiver reminders.** Two days before a waiver expires, the notifier posts one reminder
(`⏳ waiver SEC-118 on ledger-service expires ...; fix it or ask secops to renew`), so the
block that follows isn't a surprise. Locally, with no Slack webhook, it goes to the notifier log.

**Portal.** http://localhost:8080/portal shows active waivers (create or revoke them there),
rules switched off, break-glass in the last 7 days, each service's tier, owner and team policy
version, and recent decisions. Changes use a pasted token locally; in production the portal
would sit behind company SSO.

## How CQP itself is observed

Each service exposes `/metrics` (the APIs on their own port, the workers on 9100). Prometheus
scrapes them, and `ops/prometheus/alerts.yml` turns the SLOs into alerts:

| Metric | What it tells you |
| --- | --- |
| `cqp_decision_seconds` | How long a gate waits for an answer (SLO: p99 under 1s) |
| `cqp_decision_errors_total` | Failed decision requests (SLO: 99.9% available) |
| `cqp_decisions_total{stage,status,outcome}` | Outcomes by stage |
| `cqp_findings_total{rule,effect}` | Which rules fire; a jump after a release means a bad rule |
| `cqp_evidence_delay_seconds` | Upload to stored signal (SLO: 99% under 60s) |
| `cqp_store_events_total{result}` | Stored vs parked in the dead-letter topic |
| `cqp_ingest_events_total{source,result}` | Evidence accepted, or rejected for a bad token or bad input |
| `cqp_decision_publish_failures_total` | Decision events not reaching Kafka |
| `cqp_notifications_total{target,result}` | GitLab and Slack calls: ok, skipped or error |
| `cqp_waiting_gate_jobs`, `cqp_gate_retries_total` | Gates waiting on evidence, and gates resumed |
| `cqp_break_glass_total{stage}`, `cqp_kill_switches_active` | Emergency overrides and rules switched off |

| Alert | Severity | Fires when |
| --- | --- | --- |
| `CQPServiceDown` | page | A service hasn't been scraped for 2 minutes |
| `DecisionAPIErrors` | page | More than 1% of decision requests fail for 5 minutes |
| `DecisionLatencyHigh` | page | Decision p99 is over 1s for 10 minutes |
| `EvidenceDelayed` | page | Evidence p99 takes over 60s to be stored |
| `TooManyBlocks` | page | Over half of final decisions are blocked (likely a bad rule release) |
| `DeadLetterGrowing` | ticket | Any report was parked in the last 15 minutes |
| `DecisionEventsNotPublished` | ticket | Decision events failed to reach Kafka |
| `NotifierFailing` | ticket | GitLab or Slack calls are failing |
| `GateJobsPilingUp` | ticket | More than 50 gates waiting for 30 minutes |
| `BreakGlassUsed` | ticket | Someone used break-glass in the last hour |
| `RuleSwitchedOffTooLong` | ticket | A rule has been switched off for over 4 hours |

Page means delivery is affected now; ticket means fix it soon. Each alert carries a one-line
runbook. Test the rules with Prometheus's own tool:

```bash
docker run --rm -v "$PWD/ops/prometheus:/p" --entrypoint promtool prom/prometheus:v3.1.0 test rules /p/alerts_test.yml
```

## Layout

| Path | What it is |
| --- | --- |
| `src/cqp/gather/cli.py` | `cqp report`, the CI step |
| `src/cqp/gather/app.py` | Ingestion API: `/v1/reports` and `/v1/hooks/{tool}` |
| `src/cqp/gather/adapters.py` | Webhook adapters and signature check |
| `src/cqp/gather/event.py` | Raw event shape, allowed checks and formats, event ID |
| `src/cqp/gather/auth.py` | GitLab OIDC token verification |
| `src/cqp/gather/server.py` | Wiring to real Kafka and storage |
| `src/cqp/store/normalize.py` | Raw event to standard signal, one parser per format |
| `src/cqp/store/db.py` | Postgres schema, idempotent save, latest result per check |
| `src/cqp/store/worker.py` | Kafka consumer: normalize, save, commit; bad events to the DLQ |
| `src/cqp/decide/engine.py` | Completeness and "strictest finding wins" (no I/O) |
| `src/cqp/decide/policy.py` | Loads the pinned policy; talks to the OPA sidecar |
| `src/cqp/decide/app.py` | Decision API: decisions, team policies, waivers, kill switches, break-glass, portal, `replay()`, `backtest()` |
| `src/cqp/decide/db.py` | Insert-only decisions, team policy versions, waivers, kill switches, break-glass |
| `src/cqp/decide/portal.py` | The portal page |
| `src/cqp/decide/team.py` | Shape of a team policy file |
| `src/cqp/decide/team_cli.py` | `cqp policy`, `cqp waiver`, `cqp kill-switch`, `cqp break-glass` |
| `src/cqp/decide/gate.py` | `cqp gate`, the CI step |
| `src/cqp/notify/worker.py` | Notifier: render the comment, handle decisions, sweep waiting jobs |
| `src/cqp/notify/gitlab.py` | MR comment upsert, commit status, job retry |
| `src/cqp/notify/db.py` | Waiting gate jobs |
| `src/cqp/kafka.py` | Kafka publisher shared by the services |
| `policy/` | `baseline.rego`, `expectations.yaml`, `catalog.yaml` (tier, owner, repo), `trust.yaml` (deployers) |
| `src/cqp/catalog.py` | Service catalog: which repo owns a service, who may deploy |
| `ci/quality-report.gitlab-ci.yml` | The golden-path templates: report, gate and team policy |
| `examples/team-policy.yaml` | A sample `.quality/policy.yaml` |

## Run it

Needs [uv](https://docs.astral.sh/uv/).

```bash
uv sync                    # create .venv and install
uv run pytest              # run the tests
```

The store and decide tests start a real Postgres and a real OPA in Docker and are skipped if
Docker isn't running. On Colima, disable the testcontainers cleanup container, or they are
skipped:

```bash
TESTCONTAINERS_RYUK_DISABLED=true uv run pytest
```

Run the store worker:

```bash
export CQP_KAFKA=localhost:9092
export CQP_DATABASE_URL=postgresql://cqp:cqp@localhost:5432/cqp
uv run cqp-store           # creates the tables on start
```

Run the Decision API with OPA as a sidecar:

```bash
docker run -d -p 8181:8181 openpolicyagent/opa:1.0.0 run --server --addr 0.0.0.0:8181
export CQP_OPA_URL=http://localhost:8181
export CQP_POLICY_DIR=policy
uv run uvicorn cqp.decide.server:app --port 8090   # loads the policy into OPA on start
```

Run the notifier:

```bash
export CQP_GITLAB_API=https://gitlab.example.com/api/v4
export CQP_GITLAB_TOKEN=...                      # bot token with api scope
export CQP_SLACK_WEBHOOK=https://hooks.slack.com/... # optional
uv run cqp-notify
```

Run the API against a local Kafka:

```bash
export CQP_JWKS_URL=https://gitlab.example.com/oauth/discovery/keys
export CQP_KAFKA=localhost:9092
export CQP_WEBHOOK_SECRETS='{"pact-broker": "change-me"}'
uv run uvicorn cqp.gather.server:app --port 8080
```

## Use it in a pipeline

```yaml
include:
  - project: platform/golden-path
    file: ci/quality-report.gitlab-ci.yml

unit-tests:
  extends: .quality-report
  variables:
    CQP_CHECK: tests.unit
    CQP_FORMAT: junit
    CQP_FILES: "reports/**/*.xml"
  script:
    - make test            # the team's own command, unchanged
```

The build job writes the image digest to `.cqp/image-digest`, so later reports are tied to the image.

## Add a webhook adapter

1. Add a function to `adapters.py` that turns the tool's payload into `raw_event(...)` calls.
2. Register it in `ADAPTERS` and add the tool's secret to `CQP_WEBHOOK_SECRETS`.
3. Add a test in `tests/test_webhooks.py` with a sample payload.

## Assumptions and next steps

- GitLab CI with `id_tokens`. GitHub Actions works the same way with `id-token: write`.
- Raw files are stored on local disk; production uses S3.
- One worker both normalizes and saves; a separate "normalized" topic can be added when a second
  consumer needs it.
- **Not built yet:**
  - backfill of reports kept in CI artifacts;
  - retry topics, DLQ replay and monthly table partitions;
  - SSO for the portal and a group lookup for roles (a `cqp_role` token claim stands in);
  - automatically retrying the gate job after break-glass (re-run it by hand today);
  - pulling health from the monitoring API on demand when the last update is stale (stale counts
    as stale today);
  - linking incidents back to the decision that let the change through, and the nightly re-scan
    of running images;
  - "new vs existing" findings: any failing scan counts today;
  - flaky-test quarantine;
  - tier-1 local checks in the gate's fallback;
  - Slack routing per owning team (one webhook today);
  - deploy-system integration (its pre-deploy hook and approval step);
  - for observability:
    - Grafana dashboards (use the Prometheus UI for now);
    - Alertmanager routing pages and tickets to on-call;
    - Kafka consumer-lag export (`cqp_evidence_delay_seconds` covers the user-facing SLO);
    - tracing;
    - the gate's fallback rate, which happens inside CI where Prometheus can't see it.
- The resume sweep reads stored evidence every 10s instead of listening to an evidence stream.
  At this scale that is simpler, and the delay is at most 10s.
