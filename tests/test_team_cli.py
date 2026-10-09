"""`cqp policy` and `cqp waiver` against a fake CQP.

Unlike `cqp report`, these fail the job when refused.
"""

import httpx
from cqp.decide import team_cli

BACKTEST = {
    "decisions": 3,
    "changed": 1,
    "examples": [
        {"stage": "integration", "was": "PROCEED", "would_be": "BLOCKED", "decision_id": "d1"}
    ],
}


def mock(status, body):
    # Fake CQP that always answers with this status and body.
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def policy_file(tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text("thresholds:\n  perf.load: {max: 500, effect: block}\n")
    return str(f)


def test_check_is_a_dry_run_and_prints_the_backtest(tmp_path, capsys):
    client, calls = mock(200, {"version": "abc", "backtest": BACKTEST})
    assert (
        team_cli.policy("check", "payments-api", policy_file(tmp_path), "https://cqp", "t", client)
        == 0
    )
    # check never publishes: it only validates and reports the backtest.
    assert calls[0].url.params["dry_run"] == "true"
    out = capsys.readouterr().out
    assert "1 of the last 3" in out and "PROCEED -> BLOCKED" in out


def test_invalid_policy_fails_the_job_with_the_reasons(tmp_path, capsys):
    client, _ = mock(422, {"detail": {"errors": ["scan.sca at build can't be looser"]}})
    assert (
        team_cli.policy(
            "publish", "payments-api", policy_file(tmp_path), "https://cqp", "t", client
        )
        == 1
    )
    assert "can't be looser" in capsys.readouterr().err


def test_refused_waiver_fails(capsys):
    client, _ = mock(403, {"detail": {"errors": ["only approvers can create waivers"]}})
    assert (
        team_cli.waiver(
            "payments-api", "baseline.tests.e2e", "flaky", "PAY-1", 7, "https://cqp", "t", client
        )
        == 1
    )
    assert "only approvers" in capsys.readouterr().err
