# 1. How a CI job calls CQP

[← All flows](README.md) · Step 1 of 12

The CI template (`ci/quality-report.gitlab-ci.yml`) contains no URLs. It runs `cqp` commands, and
the CLI builds each request from **environment variables that GitLab sets in every job**:

| What | Where it's set | Who sets it |
| --- | --- | --- |
| Base URL (`https://cqp.company.com`) | `CQP_URL` CI/CD variable | An admin, once, at the GitLab group level |
| Endpoint path (`/v1/reports` etc.) | Written in the CLI code | Fixed; nobody configures it |
| Token | `CQP_TOKEN`, from `id_tokens:` | GitLab, freshly for every job |
| Service | `CI_PROJECT_NAME`, unless `--service` is given | GitLab (the repo name, e.g. `payments-api`) |
| MR number | `CI_MERGE_REQUEST_IID` | GitLab, in MR pipelines only |
| Image | `.cqp/image-digest` | The build job writes it; later jobs get it as an artifact |
| Event IDs | `.cqp/events/*.txt` | `cqp report` writes them; the gate job gets them as artifacts |

## The base URL: `CQP_URL`

Set once per group: GitLab → the group → **Settings → CI/CD → Variables** → `CQP_URL` =
`https://cqp.company.com`. Every project in the group inherits it. Locally it is
`http://localhost:8080` (`scripts/demo.sh` exports it; Postman calls it `{{cqp_url}}`).

The CLI reads it with the token (`src/cqp/gather/cli.py:121`) and adds the path for each command:

| Command | Request | Code |
| --- | --- | --- |
| `cqp report` | `POST {CQP_URL}/v1/reports` | `src/cqp/gather/cli.py` `report()` |
| `cqp gate` | `POST {CQP_URL}/v1/decisions`, repeated every 5s while pending | `src/cqp/decide/gate.py` `ask()` |
| `cqp policy check` | `PUT {CQP_URL}/v1/team-policies/{service}?dry_run=true` | `src/cqp/decide/team_cli.py` `policy()` |
| `cqp policy publish` | `PUT {CQP_URL}/v1/team-policies/{service}?dry_run=false` | same |
| `cqp waiver` | `POST {CQP_URL}/v1/waivers` | `team_cli.waiver()` |
| `cqp kill-switch` | `POST {CQP_URL}/v1/kill-switches` | `team_cli.create()` |
| `cqp break-glass` | `POST {CQP_URL}/v1/decisions/{id}/break-glass` | `team_cli.create()` |

Revoking a waiver and clearing a kill switch have no CLI command yet: use the portal (waivers) or
call `DELETE` directly.

## The token: `id_tokens`

```yaml
id_tokens:
  CQP_TOKEN:
    aud: cqp
```

This asks GitLab: "when this job starts, create a signed JWT for audience `cqp` and put it in
`CQP_TOKEN`". The CLI sends it as `Authorization: Bearer <token>`. The token is signed by GitLab's
private key, and CQP checks it against GitLab's public keys (`CQP_JWKS_URL`), so it can't be faked.

| Claim | Meaning | CQP uses it for |
| --- | --- | --- |
| `project_path` | The repo whose pipeline is calling | Ownership: `payments/payments-api` may only report for, gate on and set policy for `payments-api` (`policy/catalog.yaml`) |
| `sha` | The commit the job runs on | The commit a report is stored under, and the commit a decision looks up |
| `job_id` | The CI job | Tells a retried job from the original; which job the notifier retries |
| `pipeline_id` | The pipeline | Routing for the notifier |
| `iat` / `exp` | Issued at / expires | `iat` is when the result was produced ("newest result wins"); `exp` rejects old tokens |
| `aud` | Who the token is for | Must be `cqp` |
| `ref_protected` | Ran on a protected branch | Needed to publish a team policy and to ask for `prod-deploy` |
| `user_login` | Who started the job | Recorded as the approver of a waiver, the creator of a kill switch or break-glass |
| `cqp_role` | Local stand-in for a group lookup: `approver`, `security`, `oncall`, `platform` | Who may waive, break glass, or switch off rules |

**Identity always comes from the token, never from the request body.** The body says *what* the
report is (service, check, format); the token says *who* sent it and *for which commit*. That's
why a job can't report a pass for another repo's service or another commit.

Locally, `scripts/dev_token.py` and Postman play GitLab's part: they sign the same claims with the
dev key in `dev/private/key.pem`, and `dev-idp` serves the matching public key.

## One job, start to finish

A team writes:

```yaml
unit-tests:
  extends: .quality-report
  variables:
    CQP_CHECK: tests.unit
    CQP_FORMAT: junit
    CQP_FILES: "reports/**/*.xml"
  script:
    - make test            # the team's own command, unchanged
```

When it runs:

1. GitLab starts the job with `CQP_URL`, a fresh `CQP_TOKEN` and `CI_PROJECT_NAME=payments-api`.
2. `make test` writes `reports/junit.xml`.
3. `after_script` runs `cqp report --check tests.unit --format junit --files "reports/**/*.xml"`.
4. The CLI sends `POST https://cqp.company.com/v1/reports` with `meta`
   `{"service": "payments-api", "check": "tests.unit", "format": "junit", "image": null}`, the file,
   and `Authorization: Bearer <CQP_TOKEN>`. It retries after 2s, 4s and 8s if CQP is unreachable,
   then gives up **without failing the job**.
5. CQP answers `{"event_id": "…"}`; the CLI appends it to `.cqp/events/<job id>.txt`.
6. `artifacts: paths: [.cqp/]` hands that file to the later jobs.

The gate job (`extends: .quality-gate`, `CQP_STAGE: pr`) then runs `cqp gate --stage pr`, reads
every event ID in `.cqp/events/`, and calls `POST /v1/decisions`.

> **Gap in the template:** it assumes the `cqp` command already exists in the job's container
> (there is no `image:` or install step). For real pipelines the CLI must be baked into the teams'
> CI base images or installed in a `before_script`.

---

## How the reports and the decision are linked

The link is the **commit**, not the MR. Every upload and the decision request carry a token for the
same commit (`sha`). CQP stores each report under that commit, and the decision looks up everything
stored for it.

| Step | Request | Commit | MR number |
| --- | --- | --- | --- |
| Unit tests upload | `POST /v1/reports` | from the token | none |
| Secrets scan upload | `POST /v1/reports` | from the token | none |
| Gate | `POST /v1/decisions` | from the token | from the body (`merge_request`) |

Once there is an image (after the build job writes `.cqp/image-digest`), reports and decisions also
carry it. The `build.provenance` report records which commit the image was built from, so tests that
ran on the commit count for the image, all the way to `prod-deploy`.

`event_ids` in the decision request are the caller's own uploads. They don't decide what counts as
evidence; they only make CQP wait (for up to 60 seconds) until those uploads are stored, so it never
decides before its own evidence has landed.

### The MR number

CQP doesn't work it out. The gate reads `CI_MERGE_REQUEST_IID` and sends it as `merge_request`
(`src/cqp/decide/gate.py:77`); outside an MR pipeline it is `null`.

- **It doesn't change the decision.** The same commit gets the same outcome with or without it.
- **It isn't in the response or the stored decision.** It goes only into the routing context of the
  event published to Kafka for the notifier (`src/cqp/decide/app.py:249`).
- **The notifier uses it to find the MR comment** to post or update. With no MR number there is no
  comment, only a commit status.
- **It's safe for the caller to send.** The project comes from the token, so a pipeline can only
  comment on MRs in its own project.

---

---

[← All flows](README.md) · [2. Upload a report →](02-upload-report.md)
