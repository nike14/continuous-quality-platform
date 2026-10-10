# 10. What CQP calls: GitLab and Slack

[← All flows](README.md) · Step 10 of 12

The notifier reads every decision from Kafka and calls GitLab with a bot token (`CQP_GITLAB_API`,
`CQP_GITLAB_TOKEN`; `src/cqp/notify/gitlab.py`):

| What | When | GitLab API |
| --- | --- | --- |
| MR comment (one per MR, found by a hidden marker and updated in place) | The decision has a `merge_request` | `GET /projects/:id/merge_requests/:mr/notes`, then `PUT …/notes/:note_id` or `POST …/notes` |
| Commit status `cqp/<stage>` | There is a commit | `POST /projects/:id/statuses/:sha` |
| Re-run a waiting gate job | A pending decision's evidence arrived or its deadline passed | `POST /projects/:id/jobs/:job_id/retry` |
| Slack message | A final `prod-deploy` decision that isn't a clean `PROCEED`; any break-glass; waiver reminders | `CQP_SLACK_WEBHOOK` |

Project, pipeline, job and commit come from the caller's token; only the MR number comes from the
request body.

In adoption mode `shadow` nothing is posted; in `advisory` the comment is marked advisory and the
status stays green (`policy/catalog.yaml`; see *Adoption modes* in the [README](../../README.md#adoption-modes)).

Locally `CQP_GITLAB_API` isn't set, so the notifier **logs** what it would post instead:
`docker compose logs -f notifier`.

---

---

[← 9. Kill switch](09-kill-switch.md) · [11. Read-only views →](11-read-only-views.md)
