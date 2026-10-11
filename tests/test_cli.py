"""`cqp report` against a fake CQP. The rule throughout: it never fails the CI job."""

import json
from pathlib import Path

import httpx
import pytest
from cqp.gather.cli import report


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    # A fresh folder that looks like a CI job after the build: one report and the image digest.
    monkeypatch.chdir(tmp_path)
    Path("reports").mkdir()
    Path("reports/junit.xml").write_text("<testsuite/>")
    Path(".cqp").mkdir()
    Path(".cqp/image-digest").write_text("sha256:ab12\n")


def client_returning(*responses):
    """Mock CQP that answers with the given status codes in order; records requests."""
    calls = []

    def handler(request):
        calls.append(request)
        # Past the end of the list, keep giving the last answer.
        status = responses[min(len(calls), len(responses)) - 1]
        if status == "down":
            raise httpx.ConnectError("refused")
        return httpx.Response(status, json={"event_id": "e-1"})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def run(client):
    return report(
        "payments-api",
        "tests.unit",
        "junit",
        "reports/*.xml",
        "https://cqp",
        "tok",
        client,
        sleep=lambda s: None,
    )


def test_uploads_reports_and_records_event_id():
    client, calls = client_returning(202)

    assert run(client) == 0
    assert calls[0].headers["Authorization"] == "Bearer tok"
    # The image digest from the build job travels with the report.
    assert b'"image": "sha256:ab12"' in calls[0].content
    assert Path(".cqp/events/local.txt").read_text() == "e-1\n"


def test_retries_server_errors_then_succeeds():
    client, calls = client_returning(503, 202)
    assert run(client) == 0 and len(calls) == 2


def test_unreachable_cqp_never_fails_the_job(capsys):
    client, calls = client_returning("down")

    assert run(client) == 0
    assert len(calls) == 4  # the first try plus three retries
    assert "backfill" in capsys.readouterr().err
    assert not Path(".cqp/events").exists()


def test_rejected_request_is_not_retried():
    client, calls = client_returning(422)  # our request is wrong; asking again won't help
    assert run(client) == 0 and len(calls) == 1


def test_no_matching_files_sends_nothing():
    client, calls = client_returning(202)
    result = report("s", "tests.unit", "junit", "nothing/*.xml", "https://cqp", "tok", client)
    assert result == 0 and calls == []


def test_records_the_reports_result_locally_even_when_cqp_is_down(monkeypatch):
    # The gate's fallback reads this if CQP can't be asked.
    monkeypatch.setenv("CI_JOB_ID", "77")
    Path("reports/junit.xml").write_text(
        '<testsuite><testcase classname="A" name="b"><failure/></testcase></testsuite>'
    )
    client, _ = client_returning("down")
    assert run(client) == 0
    result = json.loads(Path(".cqp/results/77-tests.unit.json").read_text())
    assert result == {"check": "tests.unit", "status": "fail", "failing": ["A.b"]}


def test_an_unreadable_report_is_still_uploaded_but_not_recorded_locally():
    Path("reports/junit.xml").write_text("<not xml")
    client, calls = client_returning(202)
    assert run(client) == 0
    assert len(calls) == 1 and not Path(".cqp/results").exists()
