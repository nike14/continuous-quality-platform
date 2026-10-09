"""The three GitLab calls the notifier needs: MR comment, commit status, job retry."""

import logging
from urllib.parse import quote

import httpx

from cqp.metrics import NOTIFICATIONS

MARKER = "<!-- cqp -->"  # finds our one MR comment so it can be updated in place
log = logging.getLogger(__name__)


class GitLab:
    """Talks to the GitLab API with the notifier's bot token."""

    def __init__(self, client: httpx.Client):
        self.http = client  # base_url = https://gitlab.example.com/api/v4, bot token in headers

    def _call(self, method: str, path: str, **kwargs) -> httpx.Response | None:
        """Makes one API call and counts the result."""
        try:
            r = self.http.request(method, path, **kwargs)
            if 400 <= r.status_code < 500:
                # MR closed, job gone, no access: retrying won't help, so log and move on.
                log.warning("GitLab %s %s -> %s", method, path, r.status_code)
                NOTIFICATIONS.labels("gitlab", "skipped").inc()
                return None
            r.raise_for_status()  # 5xx: let the worker crash and retry the message
        except httpx.HTTPError:
            NOTIFICATIONS.labels("gitlab", "error").inc()
            raise
        NOTIFICATIONS.labels("gitlab", "ok").inc()
        return r

    def upsert_mr_comment(self, project: str, mr: int, body: str) -> None:
        """Updates our comment on the MR, or adds it the first time."""
        notes_path = f"/projects/{quote(project, safe='')}/merge_requests/{mr}/notes"
        # One page is enough here. An MR with 100+ notes could end up with a second comment.
        notes = self._call("GET", notes_path, params={"per_page": 100})
        if notes is None:
            return
        mine = next((n for n in notes.json() if MARKER in n["body"]), None)
        if mine:
            self._call("PUT", f"{notes_path}/{mine['id']}", json={"body": body})
        else:
            self._call("POST", notes_path, json={"body": body})

    def set_status(self, project: str, sha: str, state: str, name: str, description: str) -> None:
        """Sets the cqp/<stage> status on a commit."""
        self._call(
            "POST",
            f"/projects/{quote(project, safe='')}/statuses/{sha}",
            json={"state": state, "name": name, "description": description},
        )

    def retry_job(self, project: str, job_id: str) -> None:
        """Re-runs a gate job, which then asks CQP again."""
        self._call("POST", f"/projects/{quote(project, safe='')}/jobs/{job_id}/retry")


class LogOnlyGitLab:
    """Used when no GitLab is configured (local runs): logs what would be sent."""

    def upsert_mr_comment(self, project: str, mr: int, body: str) -> None:
        """Logs the MR comment instead of posting it."""
        log.info("MR comment on %s!%s:\n%s", project, mr, body)

    def set_status(self, project: str, sha: str, state: str, name: str, description: str) -> None:
        """Logs the commit status instead of setting it."""
        log.info("commit status %s@%s: %s = %s (%s)", project, sha[:8], name, state, description)

    def retry_job(self, project: str, job_id: str) -> None:
        """Logs the retry instead of re-running the job."""
        log.info("retry job %s in %s", job_id, project)
