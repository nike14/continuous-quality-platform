"""Turning each report format into one standard signal. Pure functions, no Kafka or database."""

import json

import pytest
from cqp.store.normalize import BadEventError, normalize

# One failure, one pass, one skipped.
JUNIT = b"""<testsuite>
  <testcase classname="PaymentRefundTest" name="partial"><failure/></testcase>
  <testcase classname="LedgerTest" name="rounding"/>
  <testcase classname="LedgerTest" name="slow"><skipped/></testcase>
</testsuite>"""

# One error carrying the tool's own fingerprint, one note without.
SARIF = json.dumps(
    {
        "runs": [
            {
                "results": [
                    {"ruleId": "sql-injection", "level": "error", "fingerprints": {"v1": "fp-1"}},
                    {"ruleId": "unused-var", "level": "note"},
                ]
            }
        ]
    }
).encode()

# The critical one fails the scan; the low one alone wouldn't.
CYCLONEDX = json.dumps(
    {
        "vulnerabilities": [
            {"id": "CVE-2026-1234", "ratings": [{"severity": "critical"}]},
            {"id": "CVE-2026-0001", "ratings": [{"severity": "low"}]},
        ]
    }
).encode()

# k6's handleSummary shape, with values nested.
K6 = json.dumps({"metrics": {"http_req_duration": {"values": {"p(95)": 212.4}}}}).encode()


def event(fmt, check="tests.unit", payload=None, image=None):
    return {
        "event_id": "e-1",
        "service": "payments-api",
        "commit": "9f3c",
        "image": image,
        "check": check,
        "format": fmt,
        "run": "4412",
        "producer": "ci:payments",
        "payload": payload,
        "raw_ref": "ref",
        "received_at": "2026-10-07T10:00:00+00:00",
    }


def files(data):
    # Stands in for reading the raw files back from storage.
    return lambda ref: [("report", data)]


def test_junit_keeps_counts_and_failing_tests():
    s = normalize(event("junit"), files(JUNIT))
    assert (s["kind"], s["status"]) == ("outcome", "fail")
    assert s["payload"] == {
        "total": 3,
        "failed": 1,
        "skipped": 1,
        "failing": ["PaymentRefundTest.partial"],
    }


def test_sarif_counts_levels_and_keeps_fingerprints():
    s = normalize(event("sarif", "static.quality"), files(SARIF))
    assert (s["kind"], s["status"]) == ("findings", "fail")
    assert s["payload"] == {
        "by_level": {"error": 1, "note": 1},
        "fingerprints": ["fp-1", "unused-var"],
    }


def test_cyclonedx_counts_severities():
    s = normalize(event("cyclonedx", "scan.sca"), files(CYCLONEDX))
    assert s["status"] == "fail"
    assert s["payload"]["by_severity"] == {"critical": 1, "low": 1}


def test_k6_becomes_a_measurement():
    s = normalize(event("k6", "perf.load"), files(K6))
    assert s["kind"] == "measurement" and s["payload"]["value"] == 212.4


def test_pact_webhook_payload_is_used_as_is():
    # Webhook events carry their payload, so no files are read (hence None).
    s = normalize(event("pact", "tests.contract", {"status": "fail", "consumer": "web"}), None)
    assert s["status"] == "fail"


def test_build_provenance_records_the_image():
    s = normalize(event("json", "build.provenance", image="sha256:ab12"), None)
    assert s["payload"] == {"image": "sha256:ab12"}


@pytest.mark.parametrize("fmt,data", [("junit", b"<not xml"), ("sarif", b"{}"), ("pact-ish", b"")])
def test_unreadable_or_unknown_reports_are_bad_events(fmt, data):
    with pytest.raises(BadEventError):
        normalize(event(fmt), files(data))


def test_production_events_are_health_signals():
    s = normalize(
        event("runtime", "runtime.slo", {"status": "fail", "error_budget_remaining": 0}), None
    )
    assert (s["kind"], s["status"]) == ("health", "fail")
