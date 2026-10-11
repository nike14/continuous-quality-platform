"""Turning each report format into one standard signal. Pure functions, no Kafka or database."""

import contextlib
import json

import pytest
from cqp.store.normalize import BadEventError, normalize
from hypothesis import given, settings
from hypothesis import strategies as st

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
        "failing": ["fp-1"],
    }


def test_cyclonedx_counts_severities():
    s = normalize(event("cyclonedx", "scan.sca"), files(CYCLONEDX))
    assert s["status"] == "fail"
    assert s["payload"]["by_severity"] == {"critical": 1, "low": 1}
    assert s["payload"]["failing"] == ["CVE-2026-1234"]  # the low one doesn't fail the scan


def test_k6_becomes_a_measurement():
    s = normalize(event("k6", "perf.load"), files(K6))
    assert s["kind"] == "measurement" and s["payload"]["value"] == 212.4


def test_pact_webhook_payload_is_used_as_is():
    # Webhook events carry their payload, so no files are read (hence None).
    s = normalize(event("pact", "tests.contract", {"status": "fail", "consumer": "web"}), None)
    assert s["status"] == "fail"


@pytest.mark.parametrize("payload", [None, {}, {"status": "maybe"}, "pass"])
def test_a_webhook_format_without_a_parsed_payload_is_a_bad_event(payload):
    # Regression: a CI upload with format pact has no payload; it raised TypeError, which
    # crashed the store worker on the same message forever.
    with pytest.raises(BadEventError):
        normalize(event("pact", "tests.contract", payload), None)


def test_observed_at_is_when_the_result_was_produced_if_known():
    produced = {
        **event("pact", "tests.contract", {"status": "pass"}),
        "observed_at": "2026-10-07T09:58:00+00:00",
    }
    assert normalize(produced, None)["observed_at"] == "2026-10-07T09:58:00+00:00"
    # Events from before observed_at existed fall back to when they arrived.
    assert normalize(event("pact", "tests.contract", {"status": "pass"}), None)["observed_at"] == (
        "2026-10-07T10:00:00+00:00"
    )


def test_build_provenance_records_the_image():
    s = normalize(event("json", "build.provenance", image="sha256:ab12"), None)
    assert s["payload"] == {"image": "sha256:ab12"}


def test_trust_is_carried_and_an_old_ci_event_without_it_is_untrusted():
    provenance = event("json", "build.provenance", image="sha256:ab12")
    assert normalize({**provenance, "trusted": True}, None)["trusted"] is True
    # Written before trust was recorded: a CI upload needs proof, a signed webhook doesn't.
    assert normalize(provenance, None)["trusted"] is False
    webhook = {
        **event("pact", "tests.contract", payload={"status": "pass"}),
        "producer": "adapter:x",
    }
    assert normalize(webhook, None)["trusted"] is True


@pytest.mark.parametrize("fmt,data", [("junit", b"<not xml"), ("sarif", b"{}"), ("pact-ish", b"")])
def test_unreadable_or_unknown_reports_are_bad_events(fmt, data):
    with pytest.raises(BadEventError):
        normalize(event(fmt), files(data))


def test_production_events_are_health_signals():
    s = normalize(
        event("runtime", "runtime.slo", {"status": "fail", "error_budget_remaining": 0}), None
    )
    assert (s["kind"], s["status"]) == ("health", "fail")


# ---- Real-world shapes and hostile input ----

NESTED_JUNIT = b"""<testsuites>
  <testsuite name="a"><testcase classname="A" name="one"><error/></testcase></testsuite>
  <testsuite name="b"><testsuite name="b.inner"><testcase classname="B" name="two"/></testsuite>
  </testsuite>
</testsuites>"""


def test_junit_with_nested_suites_counts_every_test():
    s = normalize(event("junit"), files(NESTED_JUNIT))
    assert s["payload"]["total"] == 2 and s["payload"]["failing"] == ["A.one"]


def test_sarif_across_several_files_and_runs_is_added_up():
    run = {"results": [{"ruleId": "r", "level": "error"}]}
    two_runs = json.dumps({"runs": [run, run]}).encode()

    def read(ref):
        return [("a.sarif", two_runs), ("b.sarif", two_runs)]

    s = normalize(event("sarif", "static.quality"), read)
    assert s["payload"]["by_level"] == {"error": 4}


def test_cyclonedx_without_ratings_counts_as_unknown_severity():
    sbom = json.dumps({"vulnerabilities": [{"id": "CVE-1"}, {"id": "CVE-2", "ratings": []}]})
    s = normalize(event("cyclonedx", "scan.sca"), files(sbom.encode()))
    assert s["status"] == "pass" and s["payload"]["by_severity"] == {"unknown": 2}


# Any bytes at all, in any format: normalize either reads them or raises BadEventError (which
# parks the event). Anything else would crash the store worker on the same message forever.
@settings(max_examples=300, deadline=None)
@given(
    fmt=st.sampled_from(["junit", "sarif", "cyclonedx", "k6"]),
    data=st.one_of(
        st.binary(max_size=200),
        st.from_regex(rb"\{.{0,80}\}", fullmatch=True),
        st.recursive(
            st.none() | st.booleans() | st.integers() | st.text(max_size=5),
            lambda c: st.lists(c, max_size=3)
            | st.dictionaries(
                st.sampled_from(
                    [
                        "runs",
                        "results",
                        "level",
                        "vulnerabilities",
                        "ratings",
                        "severity",
                        "metrics",
                        "http_req_duration",
                        "values",
                        "p(95)",
                        "fingerprints",
                        "id",
                    ]
                ),
                c,
                max_size=3,
            ),
            max_leaves=12,
        ).map(lambda v: json.dumps(v).encode()),
    ),
)
def test_any_input_is_read_or_parked_never_a_crash(fmt, data):
    with contextlib.suppress(BadEventError):
        normalize(event(fmt), files(data))


@pytest.mark.parametrize("fmt, data", [("cyclonedx", b"null"), ("sarif", b'{"runs": [1]}')])
def test_json_of_the_wrong_shape_is_parked(fmt, data):
    # Regression (found by the test above): these raised AttributeError and crashed the worker.
    with pytest.raises(BadEventError):
        normalize(event(fmt), files(data))
