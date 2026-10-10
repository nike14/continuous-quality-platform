# 13. Testing with Postman

[← All flows](README.md) · Step 13 of 13

`postman.json` at the repo root holds every flow above as a folder, with sample data and checks on
each response (86 requests, 150 checks). Folder numbers match the step numbers of these docs, and
every request's **Docs** tab says who calls it in real life, what it sends, what to expect and what
to look at. The collection's own Docs tab explains tokens and how to read a decision.

## Setup (once)

1. Start the stack: `docker compose up -d --build`.
2. Import `postman.json`.
3. Import `dev/cqp-local.postman_environment.json` and select the **CQP local** environment (top
   right). It holds `cqp_url`, `webhook_secret` and the dev signing key, and lives in `dev/`, which
   is gitignored. If it's missing (a fresh clone), create it from the dev key:

   ```bash
   uv run python scripts/dev_token.py > /dev/null   # creates dev/private/key.pem if needed
   python3 -c "import json; k=open('dev/private/key.pem').read(); json.dump({'name': 'CQP local', '_postman_variable_scope': 'environment', 'values': [{'key': 'cqp_url', 'value': 'http://localhost:8080', 'enabled': True}, {'key': 'webhook_secret', 'value': 'local-secret', 'enabled': True}, {'key': 'dev_private_key', 'value': k, 'type': 'secret', 'enabled': True}]}, open('dev/cqp-local.postman_environment.json', 'w'), indent=2)"
   ```
4. Let Postman find the sample reports. Either set **Settings → General → Working directory** to the
   repo root, or copy them to Postman's default folder:
   `mkdir -p ~/Postman/files/examples/reports && cp examples/reports/* ~/Postman/files/examples/reports/`.

## How the collection imitates CI

| In CI | In Postman |
| --- | --- |
| `CQP_URL` | `{{cqp_url}}` |
| `CQP_TOKEN` from `id_tokens` | Each request's **Authorization** tab: JWT Bearer, RS256, signed with `{{dev_private_key}}`. The payload shows the project, commit and role it claims |
| A new commit | The first request of each folder creates a new `*_sha` (and image) |
| A new CI job | Every request gets a new `job_id` and `iat` |
| `.cqp/events/*.txt` | Upload responses are collected in `{{event_ids}}` |
| `CI_MERGE_REQUEST_IID` | `"merge_request": 7` in the body |
| A tool signing its webhook | The collection script signs the body into `{{webhook_signature}}` |

## Order

Run folders top to bottom, and requests inside a folder in order: later requests use IDs saved by
earlier ones. Each folder starts a new change, so any folder can be run again on its own.

| Folder | Doc | Outcomes you should see |
| --- | --- | --- |
| 00 · Start here | | `200`, `200` |
| 03 · PR gate | [3](03-pr-gate.md) | `BLOCKED`, then `PROCEED` |
| 04 · Build gate | [4](04-build-gate.md) | `BLOCKED` → approver waiver refused (`403`) → security waiver → `PROCEED_WITH_WARNING` → revoked → `BLOCKED` → break-glass → `PROCEED_WITH_WARNING` |
| 05 · Team policy | [5](05-team-policy.md) | Dry run `200`, loosening `422`, publish `200`, then `REQUIRE_VALIDATION` (advisory mode) |
| 06 · Pending decision | [6](06-pending-and-resume.md) | `pending`, then `PROCEED` |
| 07 · Production deploy | [7](07-production-deploy.md), [8](08-webhooks.md) | Own pipeline `403` → `PROCEED` → incident `REQUIRE_VALIDATION` → fix `PROCEED_WITH_WARNING` → no budget `REQUIRE_VALIDATION` → freeze `BLOCKED` → canary failed `PROCEED_WITH_WARNING` |
| 08 · Webhook (Pact Broker) | [8](08-webhooks.md) | `202` |
| 09 · Kill switch | [9](09-kill-switch.md) | Non-platform `403`, then `PROCEED_WITH_WARNING` while switched off, `BLOCKED` after |
| 10 · Rules changed mid-flow | [10](10-rules-mid-flow.md) | `PROCEED` → team publishes a limit → `REQUIRE_VALIDATION` → first decision still `PROCEED` → waiver → `PROCEED_WITH_WARNING` |
| 12 · Read-only views | [12](12-read-only-views.md) | `200`s |
| 99 · Refused requests | [1](01-ci-integration.md), [2](02-upload-report.md) | The status in each request's name: `401`, `403`, `404` or `422` |

Docs steps 1, 2, 11 and 13 have no folder of their own: uploads (step 2) happen inside every
folder, notifications (step 11) happen in the background (`docker compose logs -f notifier`), and
step 13 is this page.

## Tips

- If a decision comes back `pending` right after an upload, the store worker hasn't saved it yet.
  Send it again.
- In folder 07, send **Freeze OFF** (request 17) straight after the freeze decision: the freeze is
  organisation-wide.
- Production health counts as fresh for 5 minutes after an SLO update. If a decision shows
  `baseline.production_health`, re-send the SLO request.
- Read `findings` in each decision response for the reason behind the outcome. **View → Show
  Postman Console** prints each gate's outcome and findings as you go.
- Run everything at once: right-click the collection → **Run collection**, delay 1000 ms. Or from a
  terminal:

  ```bash
  npx newman run postman.json --working-dir . -e dev/cqp-local.postman_environment.json --delay-request 1000
  ```

---

[← 12. Read-only views](12-read-only-views.md) · [All flows →](README.md)
