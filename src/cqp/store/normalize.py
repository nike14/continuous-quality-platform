"""Turns a raw event into one standard signal: kind, status and a small payload."""

import json
from collections import Counter

from defusedxml import ElementTree  # reports are untrusted input

Files = list[tuple[str, bytes]]


class BadEventError(Exception):
    """The event can never be processed (unknown format, unreadable report). Goes to the DLQ."""


def junit(files: Files):
    """Test results: counts, plus the names of failing tests."""
    total = failed = skipped = 0
    failing = []
    for _, data in files:
        for case in ElementTree.fromstring(data).iter("testcase"):
            total += 1
            # <error> means the test crashed rather than asserted; it still failed.
            if case.find("failure") is not None or case.find("error") is not None:
                failed += 1
                failing.append(f"{case.get('classname')}.{case.get('name')}")
            elif case.find("skipped") is not None:
                skipped += 1
    payload = {"total": total, "failed": failed, "skipped": skipped, "failing": failing}
    return "outcome", "fail" if failed else "pass", payload


def sarif(files: Files):
    """Static-analysis results: counts by level, plus fingerprints."""
    by_level: Counter[str] = Counter()
    fingerprints: list[str] = []
    for _, data in files:
        for run in json.loads(data)["runs"]:
            for r in run.get("results", []):
                by_level[r.get("level", "warning")] += 1
                # Prefer the tool's own fingerprint so "new vs existing" survives line shifts.
                fingerprints.append(next(iter(r.get("fingerprints", {}).values()), r.get("ruleId")))
    return (
        "findings",
        "fail" if by_level["error"] else "pass",
        {"by_level": dict(by_level), "fingerprints": fingerprints},
    )


def cyclonedx(files: Files):
    """Vulnerability scan (SBOM): counts by severity, plus CVE IDs."""
    by_severity: Counter[str] = Counter()
    ids: list[str] = []
    for _, data in files:
        for v in json.loads(data).get("vulnerabilities", []):
            # First rating is the scanner's own. Some entries have none at all.
            by_severity[(v.get("ratings") or [{}])[0].get("severity", "unknown")] += 1
            ids.append(v["id"])
    bad = by_severity["critical"] or by_severity["high"]  # medium and low are reported, not failed
    return (
        "findings",
        "fail" if bad else "pass",
        {"by_severity": dict(by_severity), "fingerprints": ids},
    )


def k6(files: Files):
    """Load test: the p95 response time."""
    metric = json.loads(files[0][1])["metrics"]["http_req_duration"]
    p95 = metric.get("values", metric)["p(95)"]  # handleSummary nests under "values"
    # Pass/fail against a baseline is a policy question, decided later.
    return "measurement", "pass", {"metric": "p95_ms", "value": p95, "unit": "ms"}


PARSERS = {"junit": junit, "sarif": sarif, "cyclonedx": cyclonedx, "k6": k6}


# Webhook formats arrive already parsed by their adapter; only the kind is needed here.
WEBHOOK_KINDS = {"pact": "outcome", "argo": "health", "runtime": "health"}


def normalize(event: dict, read_raw) -> dict:
    """Turns one raw event into a signal, or raises BadEventError if it can't be read."""
    if event["check"] == "build.provenance":
        # Nothing to parse: the event itself is the record that this image came from this commit.
        kind, status, payload = "outcome", "pass", {"image": event["image"]}
    elif event["format"] in WEBHOOK_KINDS:  # webhook adapters already send a parsed payload
        kind, status, payload = (
            WEBHOOK_KINDS[event["format"]],
            event["payload"]["status"],
            event["payload"],
        )
    elif event["format"] in PARSERS:
        try:
            kind, status, payload = PARSERS[event["format"]](read_raw(event["raw_ref"]))
        # AttributeError: valid JSON of the wrong shape, like a report that is just null.
        except (
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            AttributeError,
            ElementTree.ParseError,
        ) as e:
            raise BadEventError(f"unreadable {event['format']} report: {e}") from e
    else:
        raise BadEventError(f"no parser for format {event['format']!r}")

    return {
        "event_id": event["event_id"],
        "service": event["service"],
        "commit": event["commit"],
        "image": event["image"],
        "check": event["check"],
        "kind": kind,
        "status": status,
        "run": event["run"],
        "producer": event["producer"],
        "payload": payload,
        "raw_ref": event["raw_ref"],
        "observed_at": event["received_at"],
    }
