"""Contracts between the services: real Decision API output fed to the real notifier and gate.

The other notifier and gate tests build decision events by hand; these catch a renamed or
missing field that those would miss.
"""

from datetime import timedelta

import pytest
from conftest import CLAIMS, T0
from cqp.decide.gate import gate
from cqp.notify import db as notify_db
from cqp.notify.worker import handle, sweep
from test_decide_api import ask, record
from test_notify import FakeGitLab


def test_a_published_decision_drives_the_mr_comment_and_status(setup, conn, published):
    api, _, _ = setup
    record(conn, "tests.unit", "fail", {"failed": 1, "failing": ["PaymentRefundTest.partial"]})
    record(conn, "scan.secrets")
    ask(api, "pr", merge_request=7)

    gitlab = FakeGitLab()
    handle(published[0][2], gitlab, conn)  # exactly what went to Kafka

    project, mr, body = gitlab.comments[0]
    assert (project, mr) == (CLAIMS["project_path"], 7)
    assert "PaymentRefundTest.partial" in body
    assert gitlab.statuses == [("failed", "cqp/pr")]


def test_a_published_pending_decision_is_resumed_when_evidence_lands(setup, conn, published):
    api, _, _ = setup
    record(conn, "tests.integration")  # perf.load still running
    ask(api, "integration")

    gitlab = FakeGitLab()
    handle(published[0][2], gitlab, conn)
    assert notify_db.waiting(conn)[0][1] == str(CLAIMS["job_id"])  # the asking job, from the token

    record(conn, "perf.load", payload={"metric": "p95_ms", "value": 200, "unit": "ms"})
    sweep(conn, gitlab, T0 + timedelta(minutes=1))
    assert gitlab.retried == [str(CLAIMS["job_id"])]


# The gate passes a timeout, which Starlette's test client warns about; real clients need it.
@pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")
def test_the_gate_reads_the_real_api_answer(setup, conn, tmp_path, monkeypatch):
    # The gate's HTTP client is the API's own test client, so request and response are real.
    api, _, _ = setup
    monkeypatch.chdir(tmp_path)
    record(conn, "tests.unit", "fail", {"failed": 1, "failing": ["A.b"]})
    record(conn, "scan.secrets")
    code = gate("payments-api", "pr", "http://testserver", "good-token", api, sleep=lambda s: None)
    assert code == 1  # BLOCKED
