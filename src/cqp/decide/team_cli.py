"""`cqp policy`, `waiver`, `approve`, `kill-switch`, `break-glass`, `explain`, `preview` and
`baseline backtest`.

Unlike `cqp report`, these fail loudly: a bad policy file should fail its MR.
"""

import glob
import json
import sys
from pathlib import Path

import httpx
import yaml


def _show_errors(r: httpx.Response) -> int:
    """Prints why CQP refused, and returns 1 to fail the job."""
    # CQP answers with {"errors": [...]} or a plain message; print each on its own line.
    detail = r.json().get("detail", r.text)
    errors = detail.get("errors", [detail]) if isinstance(detail, dict) else [detail]
    for e in errors:
        print(f"cqp: {e}", file=sys.stderr)
    return 1


def policy(action: str, service: str, file: str, url: str, token: str, client: httpx.Client) -> int:
    """check: validate and backtest (on MRs). publish: make it live (default branch only)."""
    body = yaml.safe_load(Path(file).read_text()) or {}  # an empty file means "no team rules"
    r = client.put(
        f"{url}/v1/team-policies/{service}",
        params={"dry_run": action == "check"},
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )  # backtest takes a moment
    if r.status_code != 200:
        return _show_errors(r)
    result = r.json()
    bt = result["backtest"]
    verb = "published" if action == "publish" else "valid"
    print(
        f"{file} {verb} (version {result['version']}). "
        f"{bt['changed']} of the last {bt['decisions']} decisions would change."
    )
    for ex in bt["examples"]:
        print(f"  {ex['stage']}: {ex['was']} -> {ex['would_be']} ({ex['decision_id']})")
    return 0


def create(
    path: str, body: dict, show: tuple[str, ...], url: str, token: str, client: httpx.Client
) -> int:
    """POSTs a waiver, kill switch or break-glass and prints the fields worth keeping."""
    r = client.post(
        f"{url}{path}", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=10
    )
    # Anything but 201 means refused: print why and fail the command.
    if r.status_code != 201:
        return _show_errors(r)
    # Printed as JSON, so a script can pick out the ID (seed.sh does).
    print(json.dumps({k: r.json()[k] for k in show}))
    return 0


def waiver(
    service: str,
    rule: str,
    reason: str,
    ticket: str,
    days: int,
    url: str,
    token: str,
    client: httpx.Client,
    items: list[str] | None = None,
) -> int:
    """`cqp waiver`: asks CQP to accept a finding for a number of days."""
    body = {"service": service, "rule": rule, "reason": reason, "ticket": ticket, "days": days}
    return create(
        "/v1/waivers",
        {**body, "items": items or []},
        ("waiver_id", "rule", "items", "ticket", "expires_at"),
        url,
        token,
        client,
    )


def hint(f: dict, decision_id: str, outcome: str = "REQUIRE_VALIDATION") -> str | None:
    """What a person can do about one finding, or None when there's nothing to do."""
    rule, effect = f["rule"], f["effect"]
    if rule == "baseline.passed_on_rerun":
        return (
            "same code, different result: if it's flaky, quarantine it in .quality/policy.yaml "
            "under a ticket (candidates: GET /v1/flaky?service=...)"
        )
    if effect == "warn":
        return None  # nothing stops the change; already waived, approved or just worth knowing
    if rule == "baseline.change_freeze":
        return "nothing ships during a freeze; for an emergency: cqp break-glass --decision ..."
    if rule == "baseline.evidence_required":
        return "the check didn't report in time: check its job ran and its `cqp report` step worked"
    if effect == "validate" and outcome == "BLOCKED":
        return "needs an approver too, but only once nothing blocks: deal with the blocks first"
    if effect == "validate":
        return (
            f"an approver of this service (not you) can accept it: cqp approve --decision "
            f"{decision_id} --reason '...'; then re-run the gate job"
        )
    if rule.startswith("baseline.scan."):
        return (
            f"fix it (upgrade or remove the dependency), or ask security for a waiver that names "
            f"the items: cqp waiver --rule {rule} --item ..."
        )
    if rule.startswith("baseline.tests."):
        return "fix the tests named above; a rerun only helps if they're flaky, and CQP will say so"
    if rule.startswith("team."):
        return "your team's own rule: see .quality/policy.yaml"
    return None


def explain(decision_id: str, url: str, token: str, client: httpx.Client) -> int:
    """`cqp explain`: a decision's findings, each with what you can do about it."""
    r = client.get(
        f"{url}/v1/decisions/{decision_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if r.status_code != 200:
        return _show_errors(r)
    d = r.json()
    team = d["input"].get("team_policy_version") or "none"
    print(
        f"{d['service']} · {d['stage']} · {d['outcome']} ({d['status']}) · "
        f"policy {d['policy_version']} · team policy {team}"
    )
    for f in d["findings"]:
        print(f"  [{f['effect']}] {f['message']} ({f['rule']})")
        if h := hint(f, decision_id, d["outcome"]):
            print(f"      → {h}")
    for w in d["waiting"]:
        until = f" until {w['deadline']}" if "deadline" in w else ""
        print(f"  waiting for {w.get('check') or w.get('event_id')}{until}")
    if not d["findings"] and not d["waiting"]:
        print("  all expected evidence is in and no rule objected")
    return 0


def preview(service: str, stage: str, reports: list[str], url: str, client: httpx.Client) -> int:
    """`cqp preview`: judge local reports under the real rules before pushing. Nothing is kept."""
    from cqp.decide.gate import EXIT
    from cqp.store.normalize import PARSERS

    # Several --report flags for one check are read together, as CI would merge them.
    paths: dict[tuple[str, str], list[str]] = {}
    for spec in reports:
        check, _, rest = spec.partition(":")
        fmt, _, pattern = rest.partition(":")
        if not pattern or fmt not in PARSERS:
            print(f"cqp: --report wants CHECK:FORMAT:GLOB with FORMAT one of {sorted(PARSERS)}")
            return 1
        paths.setdefault((check, fmt), []).extend(sorted(glob.glob(pattern, recursive=True)))
    evidence = {}
    for (check, fmt), found in paths.items():
        if not found:
            print(f"cqp: no files for {check}; left out")
            continue
        kind, status, payload = PARSERS[fmt]([(Path(f).name, Path(f).read_bytes()) for f in found])
        if status == "fail" or check not in evidence:  # any failure wins, as in CI
            evidence[check] = {"kind": kind, "status": status, "payload": payload}
    r = client.post(
        f"{url}/v1/preview",
        json={"service": service, "stage": stage, "evidence": evidence},
        timeout=10,
    )
    if r.status_code != 200:
        return _show_errors(r)
    result = r.json()
    print(f"preview: {result['outcome']} at {stage} · policy {result['policy_version']}")
    for f in result["findings"]:
        print(f"  [{f['effect']}] {f['message']} ({f['rule']})")
    if result["not_checked"]:
        print(f"  not checked locally (CI will run them): {', '.join(result['not_checked'])}")
    return EXIT[result["outcome"]]


def baseline_backtest(file: str, url: str, token: str, client: httpx.Client) -> int:
    """`cqp baseline backtest`: which recent decisions a candidate baseline.rego would change."""
    r = client.post(
        f"{url}/v1/baseline/backtest",
        json={"rego": Path(file).read_text()},
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,  # one evaluation per decision
    )
    if r.status_code != 200:
        return _show_errors(r)
    bt = r.json()
    print(
        f"{file} ({bt['candidate']}, now {bt['current']}): "
        f"{bt['changed']} of the last {bt['decisions']} decisions would change."
    )
    for ex in bt["examples"]:
        print(
            f"  {ex['service']} {ex['stage']}: {ex['was']} -> {ex['would_be']} "
            f"({ex['decision_id']})"
        )
    return 0
