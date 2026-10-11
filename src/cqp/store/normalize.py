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
    """Static-analysis results: counts by level, plus fingerprints (and those that fail)."""
    by_level: Counter[str] = Counter()
    fingerprints: list[str] = []
    failing: list[str] = []
    for _, data in files:
        for run in json.loads(data)["runs"]:
            for r in run.get("results", []):
                level = r.get("level", "warning")
                by_level[level] += 1
                # Prefer the tool's own fingerprint so "new vs existing" survives line shifts.
                fp = next(iter(r.get("fingerprints", {}).values()), r.get("ruleId"))
                fingerprints.append(fp)
                if level == "error":
                    failing.append(fp)  # what a waiver names to accept exactly this finding
    return (
        "findings",
        "fail" if failing else "pass",
        {"by_level": dict(by_level), "fingerprints": fingerprints, "failing": failing},
    )


def cyclonedx(files: Files):
    """Vulnerability scan (SBOM): counts by severity, plus CVE IDs."""
    by_severity: Counter[str] = Counter()
    ids: list[str] = []
    failing: list[str] = []
    for _, data in files:
        for v in json.loads(data).get("vulnerabilities", []):
            # First rating is the scanner's own. Some entries have none at all.
            severity = (v.get("ratings") or [{}])[0].get("severity", "unknown")
            by_severity[severity] += 1
            ids.append(v["id"])
            if severity in ("critical", "high"):  # medium and low are reported, not failed
                failing.append(v["id"])  # what a waiver names to accept exactly this CVE
    return (
        "findings",
        "fail" if failing else "pass",
        {"by_severity": dict(by_severity), "fingerprints": ids, "failing": failing},
    )


def k6(files: Files):
    """Load test: the p95 response time."""
    metric = json.loads(files[0][1])["metrics"]["http_req_duration"]
    p95 = metric.get("values", metric)["p(95)"]  # handleSummary nests under "values"
    # Pass/fail against a baseline is a policy question, decided later.
    return "measurement", "pass", {"metric": "p95_ms", "value": p95, "unit": "ms"}


def schema_compat(files: Files):
    """Schema-registry compatibility, one file per subject: the registry's answer to
    POST /compatibility/subjects/<subject>/versions/latest, saved as <subject>.json."""
    failing: list[str] = []
    messages: list[str] = []
    for name, data in files:
        answer = json.loads(data)
        compatible = answer["is_compatible"]
        if not isinstance(compatible, bool):
            raise ValueError("is_compatible is not true or false")
        subject = name.removesuffix(".json")
        if not compatible:
            failing.append(subject)  # what a waiver names, like a CVE ID
            messages += [f"{subject}: {m}" for m in answer.get("messages", [])]
    return (
        "outcome",
        "fail" if failing else "pass",
        # The registry's own reasons, capped: enough to say what broke, never a whole schema.
        {
            "total": len(files),
            "failed": len(failing),
            "failing": failing,
            "messages": messages[:10],
        },
    )


# Steps that break the running version or lose data: they fail the migration check.
DESTRUCTIVE = {
    "ban-drop-column",
    "ban-drop-table",
    "ban-drop-database",
    "ban-drop-not-null",
    "renaming-column",
    "renaming-table",
    "changing-column-type",
}
# Steps that hold long locks on a busy table: worth a person's look, not a block.
LOCKING = {
    "require-concurrent-index-creation",
    "require-concurrent-index-deletion",
    "adding-field-with-default",
    "adding-foreign-key-constraint",
    "adding-not-nullable-field",
    "adding-required-field",
    "constraint-missing-not-valid",
    "disallowed-unique-constraint",
}


def squawk(files: Files):
    """Postgres migration lint (`squawk --reporter json`): destructive steps fail the check;
    long-lock steps are listed for the baseline's locking rule; style rules are only counted."""
    failing: list[str] = []
    locking: list[str] = []
    total = 0
    for _, data in files:
        for v in json.loads(data):
            total += 1
            where = f"{v['rule_name']} {v['file']}:{v['line']}"
            if v["rule_name"] in DESTRUCTIVE:
                failing.append(where)
            elif v["rule_name"] in LOCKING:
                locking.append(where)
    return (
        "findings",
        "fail" if failing else "pass",
        {"total": total, "failing": failing, "locking": locking},
    )


PARSERS = {
    "junit": junit,
    "sarif": sarif,
    "cyclonedx": cyclonedx,
    "k6": k6,
    "schema-compat": schema_compat,
    "squawk": squawk,
}


# Webhook formats arrive already parsed by their adapter; only the kind is needed here.
WEBHOOK_KINDS = {"pact": "outcome", "argo": "health", "runtime": "health"}


def normalize(event: dict, read_raw) -> dict:
    """Turns one raw event into a signal, or raises BadEventError if it can't be read."""
    if event["check"] == "build.provenance":
        # Nothing to parse: the event itself is the record that this image came from this commit.
        kind, status, payload = "outcome", "pass", {"image": event["image"]}
    elif event["format"] in WEBHOOK_KINDS:  # webhook adapters already send a parsed payload
        parsed = event.get("payload")
        if not isinstance(parsed, dict) or parsed.get("status") not in ("pass", "fail"):
            raise BadEventError(f"{event['format']} event without a parsed payload")
        kind, status, payload = WEBHOOK_KINDS[event["format"]], parsed["status"], parsed
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
        # Events written before observed_at existed only have received_at.
        "observed_at": event.get("observed_at") or event["received_at"],
        # Events written before trust was recorded: a CI upload isn't trusted without proof.
        "trusted": event.get("trusted", not event["producer"].startswith("ci:")),
    }
