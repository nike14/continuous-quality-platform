# CQP walkthrough: what happens, in which file, in what order

This guide explains the project in plain words. It follows one code change from a developer's
test run all the way to a "yes / no" answer, and names the file and function that does each step.
Read it top to bottom once; after that, use the tables as a map.

---

## 1. The one question

> "Do we have enough confidence to let this change move to the next stage?"

Teams already run tests, scanners and load tests with their own tools. CQP (Continuous Quality
Platform) does **not** replace those tools. It:

1. **collects** the results those tools already produce,
2. **turns** them into one common shape,
3. **decides**, using written rules, whether the change may move on,
4. **tells** people and pipelines the answer, and why.

The answer is always one of four outcomes:

| Outcome | Meaning | Pipeline exit code |
| --- | --- | --- |
| `PROCEED` | All good | 0 |
| `PROCEED_WITH_WARNING` | Fine, but something should be looked at | 0 |
| `REQUIRE_VALIDATION` | A person must approve (`cqp approve`) | 2 |
| `BLOCKED` | Must not move on | 1 |

While evidence is still on its way the answer is `REQUIRE_VALIDATION` with `status: pending`;
the gate exits **3** for that, and CQP re-runs the job later (step 7).

The stages a change goes through: `pr` → `build` → `integration` → `prod-deploy`.

---

## 2. The big picture

CQP is **four small programs** ("deployables"), all in this one repo and one Docker image:

```
                  ┌──────────────────────── CQP ─────────────────────────┐
 CI job           │                                                      │
 (tests, scans) ──┼─► 1. Ingestion API ──► Kafka ──► 2. Store worker ──► Postgres
 cqp report       │    src/cqp/gather/     cqp.signals.raw  src/cqp/store/   (signals)
                  │                                                │     │
 Tools outside CI │                                                │     │
 (Pact, Argo, ──► │    (same Ingestion API, webhook path)          │     │
  monitoring)     │                                                ▼     │
                  │                                                      │
 CI gate job ─────┼─► 3. Decision API ── reads evidence ─────────► Postgres
 cqp gate         │    src/cqp/decide/  ── asks rules ──► OPA (policy/baseline.rego)
                  │         │           ── saves decision ─────► Postgres (decisions)
                  │         ▼                                            │
                  │       Kafka cqp.decisions ──► 4. Notifier ──► GitLab (MR comment,
                  │                               src/cqp/notify/   commit status, retry job)
                  │                                             └─► Slack
                  └──────────────────────────────────────────────────────┘
```

| # | Program | Folder | Started by | Job in one line |
| --- | --- | --- | --- | --- |
| 1 | Ingestion API | `src/cqp/gather/` | `uvicorn cqp.gather.server:app` | Accept results, check who sent them, put them on Kafka |
| 2 | Store worker | `src/cqp/store/` | `cqp-store` | Read Kafka, turn each result into a standard "signal", save it |
| 3 | Decision API | `src/cqp/decide/` | `uvicorn cqp.decide.server:app` | Answer "may this change move on?" |
| 4 | Notifier | `src/cqp/notify/` | `cqp-notify` | Post MR comments and statuses, re-run waiting jobs, Slack |

Helpers they share:

| File | What it is |
| --- | --- |
| `src/cqp/catalog.py` | Reads `policy/catalog.yaml`: each service's tier, owner repo and adoption mode |
| `src/cqp/kafka.py` | One `publish()` function to send a message to Kafka |
| `src/cqp/metrics.py` | All Prometheus metrics, in one place |

Infrastructure (see `docker-compose.yml`): **Kafka** (message queue), **Postgres** (database),
**OPA** (rule engine), **Caddy** (`Caddyfile`, one front URL that routes to the right API),
**Prometheus** (monitoring), and `dev-idp` (fake GitLab token keys for local runs).

Why split into four? Each has different load and failure behaviour. If the notifier is down,
decisions still work. If Kafka is down, the Decision API still answers.

---

## 3. One change, start to finish

Imagine a developer opens a merge request on `payments-api`, and one unit test fails.

### Step 1: the CI job uploads its test report

**Where:** `ci/quality-report.gitlab-ci.yml` → `src/cqp/gather/cli.py`, function `report()`

- The team's job runs its tests as usual (`make test`). Nothing about the tests changes.
- The golden-path template (`.quality-report`) adds one line after the job:
  `cqp report --check tests.unit --format junit --files "reports/**/*.xml"`.
- `report()` finds the files, and POSTs them to `/v1/reports` with the job's GitLab token.
- If CQP is down it retries (0s, 2s, 4s, 8s), then **gives up without failing the job**.
  A reporting problem must never break a team's pipeline.
- On success, it saves the returned `event_id` in `.cqp/events/<job>.txt`. The gate uses this
  later to know "my own uploads".

### Step 2: the Ingestion API accepts it

**Where:** `src/cqp/gather/app.py`, function `upload_report()`

In order:

1. **Who is calling?** `src/cqp/gather/auth.py` (`TokenVerifier`) checks the GitLab CI token's
   signature. The repo, commit and job ID are read **from the token**, never from the request,
   so nobody can report results for someone else's commit.
2. **Is the request valid?** `src/cqp/gather/event.py`, class `ReportMeta`: the check name must be
   known (`tests.unit`, `scan.sca`, …) and the format one we can parse (`junit`, `sarif`,
   `cyclonedx`, `k6`, `schema-compat`, `squawk`, or `json` for build provenance only).
3. **Does this repo own the service?** `catalog.owns()` in `src/cqp/catalog.py`. `payments-api`'s
   pipeline can't report a pass for `checkout-web`.
4. **Size limit:** more than 50 MB is refused (413).
5. **Build the event:** `raw_event()` in `event.py`. Important fields:
   - `event_id` = a hash of producer, check, service, commit, run and file contents.
     Same upload twice → same ID → stored once.
   - `observed_at` = when the job started (from the token's `iat`). Used to decide which result
     is "newest", so a slow upload from an old run can't overwrite a newer run.
   - `received_at` = when it arrived. Used only to measure delay.
6. **Save the raw file:** `local_store()` in `src/cqp/gather/server.py` saves the uploaded file
   and returns a pointer to it, `raw_ref` (local disk here; object storage such as S3 in real life).
   The file itself never goes through Kafka; see "Raw report files" in section 11 for why.
7. **Publish:** to Kafka topic `cqp.signals.raw`, keyed by `service:commit`.
8. Reply `202 Accepted` with the `event_id`.

### Step 3: the store worker turns it into a signal

**Where:** `src/cqp/store/worker.py` (`process()`) → `src/cqp/store/normalize.py` (`normalize()`)
→ `src/cqp/store/db.py` (`save()`)

- The worker reads one Kafka message at a time and fetches the report through its `raw_ref`
  (`read_local()` in `worker.py`).
- `normalize()` picks a parser by format and produces a **signal** (section 5 explains the
  formats):
  `kind` (outcome / findings / measurement / health), `status` (pass / fail) and a small `payload`.

  | Parser in `normalize.py` | Reads | Keeps | Fails when |
  | --- | --- | --- | --- |
  | `junit()` | test results | total, failed, skipped, `failing` test names | any test failed |
  | `sarif()` | static analysis | counts by level, fingerprints, `failing` (error-level) | any error |
  | `cyclonedx()` | vulnerability scan | counts by severity, CVE IDs, `failing` (critical/high) | any critical or high |
  | `k6()` | load test | p95 latency in ms | never on its own; teams set limits |
  | `schema_compat()` | schema-registry answers, one `<subject>.json` each | subjects checked, `failing` (incompatible subjects), the registry's messages | any subject incompatible |
  | `squawk()` | Postgres migration lint (`squawk --reporter json`) | `failing` (destructive steps), `locking` (long-lock steps) | any destructive step; long locks raise `baseline.db.locking` (validate) |

  Percentages are never stored: "96% passed" stays as counts and names, so rules can see
  **which** tests failed.
- `save()` inserts into the `signals` table. Same `event_id` again → nothing happens (safe retries).
  A `build.provenance` signal also writes to `builds`: "this image came from this commit".
- If an event can **never** be processed (broken file, unknown format), it goes to the
  dead-letter topic `cqp.signals.dlq` instead. It then counts as **missing**, never as a pass.
- The Kafka offset is committed only **after** the database write. If the worker crashes in
  between, it reads the message again, and the idempotent insert makes that harmless.

### Step 4: the gate job asks for a decision

**Where:** `ci/quality-report.gitlab-ci.yml` (`.quality-gate`) → `src/cqp/decide/gate.py`, `gate()`

- A later job runs `cqp gate --stage pr`.
- `gate()` sends: service, stage, image digest (if the build wrote `.cqp/image-digest`), the
  event IDs from step 1, and the MR number.
- If the answer is `pending` (evidence still on its way), it asks again every 5 s, for up to
  120 s (`--max-wait`).
- It prints every finding into the job log, then exits with the code from the table in
  section 1.
- Still pending after `--max-wait`: exit 3. The notifier re-runs the job when the evidence lands.
- If CQP can't be reached (`fallback()`): stop if this pipeline's own unit, component or
  integration tests or secrets scan failed (`cqp report` wrote their pass/fail to
  `.cqp/results/`, see `local_failures()`); otherwise continue with a warning, except
  `prod-deploy`, which asks for approval (exit 2).
- When the outcome isn't a clean proceed it prints `cqp explain <decision>`.
- If CQP **refuses** the request (bad token, wrong repo): exit 1, so a misconfigured pipeline
  doesn't silently pass.

### Step 5: the Decision API decides

**Where:** `src/cqp/decide/app.py`, function `make_decision()`. This is the heart of CQP.

In order:

1. **Check the caller** (token). For `prod-deploy`, only the deploy system listed in
   `policy/trust.yaml` may ask (`catalog.is_deployer()`), never the service's own pipeline.
2. **What is this decision about?** The image if there is one, else the commit (`subject`).
3. **Read the evidence:** `latest()` in `src/cqp/store/db.py` returns the newest result per check
   for this image or commit. Results on the commit also count for an image built from it
   (via the `builds` table, written only by trusted builds). If one run sent several reports,
   any failure wins. At `prod-deploy` (`trusted_only`), a result about the image from an
   unprotected branch on another commit can only make things stricter: its pass is ignored.
4. **Drop stale results:** a security scan older than `max_age_days` (7) counts as missing.
5. **What is expected?** `policy/expectations.yaml` lists which checks each stage expects, per
   tier (tier comes from `policy/catalog.yaml`). Each has a `deadline` (minutes) and an
   `on_missing` effect. A team can add more via its team policy.
6. **Is the evidence complete?** `completeness()` in `src/cqp/decide/engine.py` splits
   not-yet-reported checks into:
   - **waiting:** still before its deadline (counted from the first time anyone asked), and
   - **missing:** deadline passed.

   The caller's own uploads (`event_ids`) that aren't stored yet are waited for during that
   gate job's first minute (`db.first_asked(..., job_id=...)`).
7. **Never decide on half the evidence.** If anything is waiting, the answer is
   `status: pending` with the list of what it's waiting for. The rules are not run yet.
8. **Gather everything the rules need** into one `policy_input`: evidence, missing checks,
   team policy, active waivers, kill switches, break-glass, approvals, and (for `prod-deploy`)
   production state from `db.runtime()`. Plus two things from other results, both in
   `store/db.py`:
   - `reruns()`: checks that pass now but failed in an earlier run of the same change
     (likely flaky);
   - `baseline()`: each check's last trusted result on another commit, which tells new findings
     from existing ones and gives measurements something to compare with.
9. **Run the rules:** `policy.evaluate()` in `src/cqp/decide/policy.py` sends the input to OPA,
   which runs `policy/baseline.rego`, and gets back a list of **findings**. Each finding has a
   `rule`, an `effect` (warn / validate / block) and a plain-English `message`.
10. **Pick the outcome:** `outcome()` in `engine.py`. The strictest finding wins:
    block → `BLOCKED`, validate → `REQUIRE_VALIDATION`, warn → `PROCEED_WITH_WARNING`,
    none → `PROCEED`. There is no score.
11. **Save it:** `db.insert()` in `src/cqp/decide/db.py`. Decisions are **never updated**, only
    added. Each row stores the exact input and the policy version, so it can be replayed later
    (`replay()` in `app.py`).
12. **Tell the notifier:** publish the decision to Kafka topic `cqp.decisions`. If Kafka is down
    the decision is still returned; only notifications lag.

In our example: `tests.unit` failed at stage `pr` → `baseline.rego` says effect `block` →
outcome `BLOCKED` → gate exits 1.

### Step 6: the rules themselves

**Where:** `policy/baseline.rego` (written in Rego, OPA's rule language)

The main parts, top to bottom:

| Part | What it says |
| --- | --- |
| `on_fail` | For each check and stage, the effect when it **fails** (e.g. failing unit tests block a PR) |
| `locked` | Cells a team may make stricter but never looser (security scans, contracts, prod tests) |
| `raw contains f if …` | One block per kind of finding: failed check, missing check, team threshold, error budget, incident, freeze, stale health… |
| `adjust`, `step`, `apply_waiver` | Then apply overrides: kill switch → waiver → break-glass |
| `team_policy_errors` | Is a team's policy file within the allowed bounds? |
| `waiver_errors`, `kill_errors`, `break_glass_errors` | Who may create which override |

Because rules live in one file, "why was this blocked?" always has a rule name and a message.

To see the rules answer real inputs (waivers, break-glass, team policies, production state), run
`./scripts/opa-examples.sh`. [OPA examples](opa-examples.md) explains each one.

**Which checks the baseline judges.** Every check CQP accepts has an `on_fail` entry, so a
failing result always produces a finding (a test enforces this):

| Check | Judged at |
| --- | --- |
| `tests.unit`, `tests.component` | block at pr, build, prod-deploy |
| `tests.integration` | block at integration, prod-deploy |
| `tests.api` | warn at pr; block at integration, prod-deploy |
| `tests.contract` | warn at pr; block at build, integration, prod-deploy |
| `tests.e2e` | warn at pr and prod-deploy; validate at integration |
| `scan.sca`, `scan.container` | warn at pr; block at build, integration, prod-deploy |
| `scan.secrets` | block at pr, build, prod-deploy |
| `static.quality` | warn at pr; block at build (error-level issues only) |
| `perf.load` | only against a team's threshold |
| `deploy.canary` | a warning on the service's next production deploy |

A check being *judged* is different from it being *expected*: `expectations.yaml` decides which
checks a stage waits for. Component, API, end-to-end and container results are judged when
they arrive, but the baseline doesn't wait for them; a team adds them under `extra_checks`.

### Step 7: people and pipelines are told

**Where:** `src/cqp/notify/worker.py` (`handle()`, `sweep()`, `remind()`) and
`src/cqp/notify/gitlab.py`

- **MR comment:** `render()` builds one comment with the outcome, each finding and what's still
  waited for. `upsert_mr_comment()` updates the same comment each time (found by a hidden
  `<!-- cqp -->` marker) instead of adding new ones.
- **Commit status:** `cqp/pr`, `cqp/build`, … success / failed / pending.
- **Slack:** only for production problems and break-glass.
- **Resume waiting gates:** for a `pending` decision, the notifier remembers the gate job in
  `waiting_pipelines` (`src/cqp/notify/db.py`). Every 10 s, `sweep()` checks whether the evidence
  has arrived or the deadline passed, then asks GitLab to **retry that job**. The job asks again
  and gets the final answer. CQP never drives the pipeline itself.
- **Waiver reminders:** two days before a waiver expires, one message is sent.

---

## 4. Results from tools outside CI (webhooks)

**Where:** `src/cqp/gather/app.py` (`webhook()`) → `src/cqp/gather/adapters.py`

Some results don't come from a CI job: Pact Broker (contract tests), Argo Rollouts (canary),
monitoring and incident tools. They POST to `/v1/hooks/<tool>`.

1. The body must be signed with that tool's shared secret (`X-CQP-Signature`);
   `valid_signature()` checks it.
2. One small **adapter** function per tool turns the tool's JSON into the same raw event shape:

   | Adapter | URL | Becomes check |
   | --- | --- | --- |
   | `pact_broker()` | `/v1/hooks/pact-broker` | `tests.contract` |
   | `argo_rollouts()` | `/v1/hooks/argo-rollouts` | `deploy.canary` |
   | `runtime()` | `/v1/hooks/runtime` | `runtime.slo`, `runtime.incident`, `org.freeze` |

3. From there it's the same path as step 2 onward: Kafka → store worker → `signals` table.

To add a new tool: write one adapter function, register it in `ADAPTERS`, add a test.

---

## 5. Which format each tool sends

Tools don't send "JUnit for everything". Each **kind** of tool sends the standard format it
already knows, and **CQP does the converting**: `normalize()` in `src/cqp/store/normalize.py`
turns every format into one common shape, the **signal**:

```
kind     outcome | findings | measurement | health
status   pass | fail
payload  small details: failing test names, CVE IDs, p95 latency…
```

| Kind of tool | Format it sends | Parser / adapter | Becomes |
| --- | --- | --- | --- |
| Tests in any language (Go, Java, JS, Python…) | **JUnit XML** | `junit()` | `outcome`: counts plus failing test names |
| Static analysis, linters, code security | **SARIF** (JSON) | `sarif()` | `findings`: counts by level plus fingerprints |
| Dependency and container vulnerability scans | **CycloneDX** (JSON) | `cyclonedx()` | `findings`: counts by severity plus CVE IDs |
| Load / performance tests | **k6 summary** (JSON) | `k6()` | `measurement`: p95 latency in ms |
| Event schemas (Avro, Protobuf) | the **schema registry's compatibility answer** per subject | `schema_compat()` | `outcome`: incompatible subjects (`contract.schema`) |
| Database migrations (Postgres) | **squawk** JSON | `squawk()` | `findings`: destructive and long-lock steps (`db.migration`) |
| Build | small JSON with the image digest | handled inside `normalize()` | `outcome`: links image to commit |
| Pact Broker, Argo Rollouts, monitoring, incident tools | their own webhook JSON | an adapter in `src/cqp/gather/adapters.py` | `outcome` / `health` |

**Why this way**

- These formats are already widely used. Most test frameworks can write JUnit, most code
  scanners write SARIF, and most dependency scanners write CycloneDX. Teams usually only add an
  output flag to a tool they already run (`--junitfile`, `--format sarif`). For example, Go
  teams use `gotestsum --junitfile reports/unit.xml -- ./...`, because `go test` doesn't write
  JUnit on its own.
- CQP only has to understand a handful of formats, not hundreds of tools.
- After `normalize()`, the rest of CQP (storage, rules, MR comments) only ever sees signals.
  It doesn't know or care which tool produced them.

**When a tool doesn't fit**

| Situation | What to do |
| --- | --- |
| The tool can write one of the formats above | Turn on that output; nothing changes in CQP |
| The tool runs outside CI and can call a URL | Write one small adapter in `adapters.py` (see section 4) |
| A new kind of data, e.g. code coverage | Add a format and parser to `normalize.py` and a rule in `baseline.rego` (coverage isn't supported today) |

An unknown format is refused at upload with a 422. If a file that was accepted turns out to be
broken, it goes to the dead-letter topic and counts as **missing** evidence, never as a pass.

---

## 6. Production signals

**Where:** `src/cqp/decide/db.py`, function `runtime()`, and the production rules in
`policy/baseline.rego`

Every `prod-deploy` decision also looks at the service's live state:

| State | Read from | Effect |
| --- | --- | --- |
| Error budget used up | newest `runtime.slo` | needs approval, unless `--fix <ticket>` |
| An incident open | each incident's newest state | needs approval, unless `--fix <ticket>` |
| A canary failed in the last 24 h | `deploy.canary` signals | warning |
| Organisation-wide change freeze | `org.freeze` | blocked |
| No fresh health data (5 min) | `runtime.slo` timestamp | approval for tier-1, warning otherwise |

The canary rollback itself is done by Argo Rollouts, not CQP. CQP only records it and lets it
shape the **next** decision.

---

## 7. How teams stay in control

| Feature | What it does | Where |
| --- | --- | --- |
| **Team policy** | A team's `.quality/policy.yaml`: wait for extra checks, tighten effects, set limits like "p95 under 150 ms". Checked and backtested on the MR, published from the main branch. | `src/cqp/decide/team.py` (shape), `put_team_policy()` and `backtest()` in `app.py`, bounds in `baseline.rego`, example in `examples/team-policy.yaml` |
| **Waiver** | "Accept this known finding for N days" (max 30). Can name exact items like a CVE ID. Still shown, only warns. | `create_waiver()` in `app.py`, `apply_waiver` in `baseline.rego`, `cqp waiver` |
| **Quarantine** | Known-flaky tests (at most 10 per check, under a ticket) only warn when they fail; anything else failing still blocks | `quarantine` in `team.py`, `quarantined()` in `baseline.rego`, candidates from `GET /v1/flaky` |
| **Approval** | An approver who isn't the author accepts exactly the `validate` findings of one decision, for 24 h | `approve()` in `app.py`, `approved()` in `baseline.rego`, `cqp approve` |
| **Kill switch** | Platform team turns one broken rule into warn-only, everywhere, for up to 72 h | `kill()` in `app.py`, `cqp kill-switch`; a baseline change is tried first with `cqp baseline backtest` |
| **Break-glass** | On-call lets one change through one stage in an emergency. Always announced in Slack. | `break_glass()` in `app.py`, `cqp break-glass` |
| **Portal** | One web page listing waivers, kill switches, break-glass, approvals, flaky test candidates, services and recent decisions | `src/cqp/decide/portal.py`, at `/portal` |
| **Preview and explain** | `cqp preview` judges local reports under the real rules and records nothing; `cqp explain` lists a decision's findings with what to do about each | `preview()` in `app.py`, `preview()`, `explain()` and `hint()` in `team_cli.py` |

The CLI commands for these live in `src/cqp/gather/cli.py` (argument parsing) and
`src/cqp/decide/team_cli.py` (the HTTP calls).

---

## 8. Rolling it out without breaking anyone

Each service has a `mode` in `policy/catalog.yaml`:

| Mode | Decision saved | Gate | Shown to developers |
| --- | --- | --- | --- |
| `shadow` | yes | always exits 0 | nothing |
| `advisory` | yes | always exits 0, logs "would be BLOCKED" | MR comment marked advisory, status stays green |
| `enforce` | yes | acts on the outcome | everything |

The outcome is computed the same way in all modes, so you can measure "how often would we have
blocked?" before turning enforcement on. The mode is applied in `gate()` (`gate.py`) and
`handle()` (`notify/worker.py`).

---

## 9. Watching CQP itself

| What | Where |
| --- | --- |
| All metrics (decision time, errors, outcomes, evidence delay, DLQ count…) | `src/cqp/metrics.py` |
| Each program exposes `/metrics` | APIs on their own port; workers on 9100 |
| Scrape config | `ops/prometheus/prometheus.yml` |
| Alert rules (e.g. too many blocks, DLQ growing, break-glass used) | `ops/prometheus/alerts.yml`, tested by `alerts_test.yml` |

---

## 10. Configuration files (the knobs)

| File | Decides |
| --- | --- |
| `policy/catalog.yaml` | Each service's tier, owner team, owning repo, adoption mode |
| `policy/expectations.yaml` | Which checks each stage waits for, per tier; deadlines; max age of scans |
| `policy/baseline.rego` | The organisation's rules: effects, locked cells, overrides, who may do what |
| `policy/trust.yaml` | Which project is the deploy system (the only one allowed to ask about production) |
| `examples/team-policy.yaml` | Example of what a team can put in `.quality/policy.yaml` |
| `ci/quality-report.gitlab-ci.yml` | The golden-path CI templates teams include |

---

## 11. Database tables

| Table | Written by | Holds |
| --- | --- | --- |
| `signals` | store worker (`store/db.py`) | Every normalised result, one row per event |
| `builds` | store worker | Image → commit it was built from |
| `decisions` | Decision API (`decide/db.py`) | Every decision, insert-only, with its full input |
| `policy_versions` | Decision API at startup | Every `baseline.rego` ever used, for replay |
| `team_policies` | Decision API | Every published team policy version |
| `waivers`, `kill_switches`, `break_glass`, `approvals` | Decision API | Overrides and their history (revoked, not deleted) |
| `waiting_pipelines` | notifier (`notify/db.py`) | Gate jobs waiting for evidence |

Kafka topics: `cqp.signals.raw` (new results), `cqp.signals.dlq` (results that couldn't be
read), `cqp.decisions` (every decision, for the notifier).

### Raw report files (object storage)

Postgres and Kafka hold only small records. The original files (JUnit XML, SARIF, CycloneDX,
k6 summaries) live in object storage, and each event carries a pointer to them, `raw_ref`.

```text
CI uploads junit.xml
  → Ingestion API saves the file, gets raw_ref        (gather/app.py, local_store() in gather/server.py)
  → Kafka event: {event_id, check, ..., raw_ref}      (small, no file inside)
  → store worker reads the file through raw_ref       (read_local() in store/worker.py)
  → Postgres signal: counts, failing names, raw_ref   (the summary rules need)
```

**Why not keep the file in Kafka or Postgres?**

- **Size.** A big test suite's JUnit file or a full SBOM can be several MB. Kafka's default
  message limit is about 1 MB, and thousands of files a day would bloat the tables decisions read.
- **Decisions only need the summary.** Counts, failing test names and CVE IDs go to Postgres;
  the rest of the file is kept, not queried.

**What keeping the original is for:**

| Use | How |
| --- | --- |
| Re-parse after a fix | A parser bug or a dead-lettered event: fix the parser, read the same file again |
| Disaster recovery | The `signals` table can be rebuilt from the raw files |
| Audit and investigation | Decision → signal → `raw_ref` → the exact report it was based on |
| Low cost | Object storage is cheap; a lifecycle rule expires old files |

**In the prototype** it's a Docker volume, `raw`, mounted at `/data/raw` (`CQP_RAW_DIR`) in both
the Ingestion API and the store worker (`docker-compose.yml`). Each upload gets a folder named after
its `event_id`. Today it's used only to hand files from ingestion to the worker; re-parsing
dead-lettered events and linking a finding to its raw report aren't built yet.

---

## 12. Tests: which file checks what

| Test file | Covers |
| --- | --- |
| `tests/test_ingestion.py`, `test_auth.py`, `test_gather_server.py` | Upload API, token checks, file storage |
| `tests/test_webhooks.py` | Webhook signatures and adapters |
| `tests/test_cli.py`, `test_cli_main.py` | `cqp report` and command parsing |
| `tests/test_normalize.py` | Every parser, including random bad input |
| `tests/test_store.py` | Saving signals, "newest result wins", DLQ |
| `tests/test_engine.py` | Waiting vs missing, outcome selection |
| `tests/test_policy.py` | The Rego rules, against a real OPA |
| `tests/test_examples.py` | The files in `examples/team-policies/` and `examples/opa/` still say what CQP does |
| `tests/test_decide_api.py` | Full decisions against real Postgres + OPA, including production state |
| `tests/test_gate.py` | Gate polling, exit codes, fallbacks |
| `tests/test_team_and_waivers.py`, `test_overrides.py`, `test_team_cli.py` | Team policies, waivers, kill switch, break-glass, portal |
| `tests/test_notify.py`, `test_gitlab.py` | MR comment, statuses, resume sweep, Slack |
| `tests/test_contract.py` | The decision event shape the notifier relies on |
| `tests/test_metrics.py`, `test_kafka.py`, `test_catalog.py` | Shared helpers |

`tests/conftest.py` starts throwaway Postgres and OPA containers and defines the fake tokens.
On Colima, run tests with `TESTCONTAINERS_RYUK_DISABLED=true uv run pytest`.

`scripts/e2e.sh` runs 50+ checks against the real running stack, including outages.
`scripts/seed.sh` fills every table with example data. `scripts/demo.sh` is the short demo from the README.

---

## 13. Suggested reading order

If you have an hour, open these in this order:

1. `ci/quality-report.gitlab-ci.yml`: what a team actually adds to its pipeline.
2. `src/cqp/gather/cli.py` → `report()`: the upload.
3. `src/cqp/gather/app.py` → `upload_report()`, and `event.py`: what an event looks like.
4. `src/cqp/store/normalize.py`: how different tools become one shape.
5. `src/cqp/store/db.py` → `latest()`: how "the current evidence" is chosen.
6. `src/cqp/decide/app.py` → `make_decision()`: the main flow.
7. `policy/expectations.yaml` and `policy/baseline.rego`: the actual rules.
8. `src/cqp/decide/gate.py`: how the answer becomes an exit code.
9. `src/cqp/notify/worker.py`: what people see afterwards.

To see it run: `docker compose up -d --build`, then `./scripts/demo.sh`, then open
http://localhost:8080/portal.

---

## 14. Words used in this project

| Word | Meaning |
| --- | --- |
| **Signal / evidence** | One stored result: "tests.unit failed for commit X" |
| **Check** | A named kind of result: `tests.unit`, `scan.sca`, `perf.load`… |
| **Stage** | Where in delivery we are: `pr`, `build`, `integration`, `prod-deploy` |
| **Subject** | What a decision is about: the image, or the commit before there is one |
| **Finding** | One reason from the rules, with an effect and a message |
| **Effect** | How serious a finding is: warn, validate, block |
| **Tier** | How critical a service is (`tier-1` gets stricter expectations) |
| **Pending** | Some expected evidence hasn't arrived yet, so no final answer |
| **Waiver / kill switch / break-glass** | The three ways to override a rule, each logged |
| **Golden path** | The shared CI/CD templates every team already uses |
| **Deployable** | One of CQP's four programs |
