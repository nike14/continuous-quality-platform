"""The GitLab client against a fake GitLab: one comment per MR, updated in place."""

import httpx
from cqp.notify.gitlab import MARKER, GitLab


def gitlab_with(notes=(), status=200):
    calls = []

    def handler(request):
        # raw_path keeps %2F as sent; .path would decode it back to "/".
        calls.append((request.method, request.url.raw_path.decode().split("?")[0]))
        if request.method == "GET":
            return httpx.Response(status, json=list(notes))
        return httpx.Response(201, json={})

    client = httpx.Client(base_url="https://gitlab/api/v4", transport=httpx.MockTransport(handler))
    return GitLab(client), calls


def test_first_decision_creates_the_comment():
    gitlab, calls = gitlab_with(notes=[{"id": 1, "body": "LGTM"}])  # a reviewer's comment, not ours
    gitlab.upsert_mr_comment("payments/payments-api", 7, f"{MARKER} hi")
    assert calls[-1] == ("POST", "/api/v4/projects/payments%2Fpayments-api/merge_requests/7/notes")


def test_later_decisions_update_the_same_comment():
    gitlab, calls = gitlab_with(notes=[{"id": 42, "body": f"{MARKER} old"}])
    gitlab.upsert_mr_comment("payments/payments-api", 7, f"{MARKER} new")
    assert calls[-1] == (
        "PUT",
        "/api/v4/projects/payments%2Fpayments-api/merge_requests/7/notes/42",
    )


def test_closed_or_missing_mr_is_skipped_not_retried():
    gitlab, calls = gitlab_with(status=404)
    gitlab.upsert_mr_comment("payments/payments-api", 7, "x")  # must not raise
    assert calls == [("GET", "/api/v4/projects/payments%2Fpayments-api/merge_requests/7/notes")]
