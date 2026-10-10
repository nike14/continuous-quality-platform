# CQP API flows

How CQP is called from the API side: who calls each endpoint in real life, what the request carries,
where each value comes from, and what CQP answers. Each flow ends with the Postman requests that act
it out, so you can test it by hand.

For what happens *inside* CQP (which file, which function), see [WALKTHROUGH.md](../WALKTHROUGH.md).

## Read in this order

The files are numbered in the order a change goes through CQP. Read them top to bottom; each one
ends with a link to the next.

| Step | Doc | Who calls | Endpoints / what it covers |
| --- | --- | --- | --- |
| 1 | [How a CI job calls CQP](01-ci-integration.md) | CI pipeline (`id_tokens`, `cqp` CLI) | Where the URL, token, service, image and MR number come from; how reports and decisions are linked |
| 2 | [Upload a report](02-upload-report.md) | CI test and scan jobs (`cqp report`) | `POST /v1/reports` |
| 3 | [PR gate: blocked, then fixed](03-pr-gate.md) | MR pipeline gate job (`cqp gate`) | `POST /v1/decisions`, `GET /v1/decisions/{id}` |
| 4 | [Build gate: waiver and break-glass](04-build-gate.md) | Build pipeline, security, on-call | `POST /v1/decisions`, `/v1/waivers`, `/v1/decisions/{id}/break-glass` |
| 5 | [Team policy](05-team-policy.md) | `quality-policy` job (`cqp policy`) | `PUT`/`GET /v1/team-policies/{service}` |
| 6 | [Pending decisions and resume](06-pending-and-resume.md) | Gate job, then the notifier | `POST /v1/decisions`, GitLab job retry |
| 7 | [Production deploy](07-production-deploy.md) | Deploy system's pipeline, monitoring, incident tool | `POST /v1/decisions` (`prod-deploy`), `/v1/hooks/runtime` |
| 8 | [Webhooks](08-webhooks.md) | Pact Broker, Argo Rollouts, monitoring, incident tool, release management | `POST /v1/hooks/{tool}` |
| 9 | [Kill switch](09-kill-switch.md) | Platform team (`cqp kill-switch`) | `POST`/`GET`/`DELETE /v1/kill-switches` |
| 10 | [When rules are set, and changing them mid-flow](10-rules-mid-flow.md) | Platform team, teams, approvers | When each kind of rule takes effect; a live example of a rule added mid-flow |
| 11 | [What CQP calls: GitLab and Slack](11-notifications.md) | The notifier | GitLab MR notes, commit statuses, job retry; Slack |
| 12 | [Read-only views](12-read-only-views.md) | Anyone, no token | `GET /portal` and the `GET` endpoints |
| 13 | [Testing with Postman](13-postman-testing.md) | You | Setup, how the collection imitates CI, the order to run it in |

- **Steps 1 and 2** are the basics every other flow builds on.
- **Steps 3 to 9** are the flows, from the first MR to production.
- **Step 10** explains when rules take effect, and what happens when they change mid-flow.
- **Steps 11 and 12** are what CQP does after deciding, and how to look at the results.
- **Step 13** is how to try every flow yourself in Postman.

## The four outcomes


| Outcome | Meaning | `cqp gate` exit code |
| --- | --- | --- |
| `PROCEED` | All good | 0 |
| `PROCEED_WITH_WARNING` | Fine, but something should be looked at | 0 |
| `REQUIRE_VALIDATION` | A person must approve, or evidence is still missing | 2 |
| `BLOCKED` | Must not move on | 1 |

## Who calls CQP, and who CQP calls

GitLab itself never calls CQP. The **CI jobs** GitLab runs call it through the `cqp` command-line
tool. Some other tools and people call it too. In the other direction, **CQP calls GitLab** to post
the results.

```
GitLab runs a job ──► cqp report ──► POST /v1/reports ────┐
GitLab runs a job ──► cqp gate ────► POST /v1/decisions ──┤
GitLab runs a job ──► cqp policy ──► PUT /v1/team-policies┼──► CQP
Pact / Argo / monitoring ─────────► POST /v1/hooks/* ─────┤
People: cqp waiver / kill-switch / break-glass, portal ───┘
                                                            │
                                                            ▼
                     notifier ──► GitLab API: MR comment, commit status, job retry
                              └─► Slack
```

| Caller | How | Endpoints |
| --- | --- | --- |
| CI test and scan jobs | `cqp report` (template `.quality-report`) | `POST /v1/reports` |
| CI gate jobs | `cqp gate` (template `.quality-gate`) | `POST /v1/decisions` |
| CI `quality-policy` job | `cqp policy check` / `cqp policy publish` | `PUT /v1/team-policies/{service}` |
| Deploy system's pipeline | `cqp gate --stage prod-deploy` | `POST /v1/decisions` |
| Pact Broker, Argo Rollouts, monitoring, incident tool, release management | Signed webhook | `POST /v1/hooks/{tool}` |
| Security and approvers | `cqp waiver`, or the portal | `POST /v1/waivers`, `DELETE /v1/waivers/{id}` |
| On-call engineers | `cqp break-glass` | `POST /v1/decisions/{id}/break-glass` |
| Platform team | `cqp kill-switch`, or the API directly | `POST /v1/kill-switches`, `DELETE /v1/kill-switches/{id}` |
| Anyone | Browser or curl, no token | `GET /portal`, `GET /v1/decisions/{id}`, `GET /v1/waivers`, `GET /v1/kill-switches`, `GET /v1/team-policies/{service}` |

All of these go to one URL. The gateway (`Caddyfile`) sends `/v1/decisions*`, `/v1/team-policies*`,
`/v1/waivers*`, `/v1/kill-switches*` and `/portal*` to the Decision API, and everything else
(`/v1/reports`, `/v1/hooks/*`, `/metrics`) to the ingestion API.

---

---

[Start: 1. How a CI job calls CQP →](01-ci-integration.md)
