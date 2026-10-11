# CQP: Continuous Quality Platform

CQP answers one question at every delivery stage: **is there enough evidence to let this change
move on?** It reads the results teams' tools already produce (JUnit, SARIF, CycloneDX, k6,
schema-registry answers, squawk migration lint, webhooks), judges them against versioned rules in OPA, and returns one of four explained outcomes
that pipelines act on: `PROCEED`, `PROCEED_WITH_WARNING`, `REQUIRE_VALIDATION` or `BLOCKED`.

This repository is a working slice of it: four deployables (ingestion API, store worker,
Decision API, notifier) from one image, plus the golden-path CLI `cqp`.

## Where to read

| You want | Read |
| --- | --- |
| Why it is designed this way: problem, architecture, trade-offs, scale, build vs buy, adoption | [docs/DESIGN.md](docs/DESIGN.md) |
| What happens in which file, following one change end to end | [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) |
| Each API call: who calls it, what it sends, what comes back | [docs/flows/](docs/flows/README.md) |
| How the rules read their input, with runnable examples | [docs/opa-examples.md](docs/opa-examples.md) |

## Run it locally

Needs Docker and [uv](https://docs.astral.sh/uv/).

```bash
uv run python scripts/dev_token.py > /dev/null   # once: creates a local signing key in dev/
docker compose up -d --build                     # Kafka, Postgres, OPA, the four deployables
./scripts/demo.sh                                # upload a failing test report, ask the PR gate
```

Expected: `cqp: BLOCKED at pr …` and `[block] tests.unit failed: PaymentRefundTest.partial`.
Then:

```bash
./scripts/seed.sh    # fill every table with varied data through the real APIs (~1 min)
./scripts/e2e.sh     # end-to-end checks against the stack, outages included (~10 min)
```

CQP is at http://localhost:8080 (portal at `/portal`, Prometheus at http://localhost:9090).

## Test

```bash
uv sync
TESTCONTAINERS_RYUK_DISABLED=true uv run pytest   # real Postgres and OPA in containers
```

CI (`.gitlab-ci.yml`) runs ruff, mypy and the tests with `CQP_REQUIRE_DOCKER=1`, so a runner
without Docker fails instead of skipping. On MRs that touch `policy/baseline.rego` it also
backtests the change against recent decisions.

## Use it in a pipeline

```yaml
include:
  - project: platform/golden-path
    file: ci/quality-report.gitlab-ci.yml

unit-tests:
  extends: .quality-report
  variables: {CQP_CHECK: tests.unit, CQP_FORMAT: junit, CQP_FILES: "reports/**/*.xml"}
  script:
    - make test            # the team's own command, unchanged
```

Gates extend `.quality-gate` with `CQP_STAGE`. Exit codes: 0 proceed (or warn), 1 blocked,
2 needs a person (`cqp approve`), 3 still waiting (the notifier re-runs the job). Before pushing,
`cqp preview --service <name> --report "tests.unit:junit:reports/*.xml"` judges local reports
under the same rules; `cqp explain <decision>` says what to do about each finding.

## Layout

| Path | What it is |
| --- | --- |
| `src/cqp/gather/` | `cqp` CLI and the ingestion API |
| `src/cqp/store/` | Parsers and the evidence store worker |
| `src/cqp/decide/` | Decision API, gate, team policy, overrides, portal |
| `src/cqp/notify/` | MR comments, commit statuses, Slack, resuming waiting gate jobs |
| `policy/` | `baseline.rego`, `expectations.yaml`, `catalog.yaml`, `trust.yaml` |
| `ci/` | The golden-path CI templates teams include |
| `examples/` | Sample tool reports, team policies, OPA inputs |
| `ops/prometheus/` | Scrape config and tested alert rules |
| `scripts/` | Dev token, demo, seed, end-to-end checks |

What is design-only (S3, SSO, signed provenance, tracing and more) is listed in
[DESIGN.md, section 14](docs/DESIGN.md#14-prototype-evidence-gaps-and-resources).
