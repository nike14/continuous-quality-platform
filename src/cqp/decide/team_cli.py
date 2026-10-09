"""`cqp policy`, `cqp waiver`, `cqp kill-switch` and `cqp break-glass`.

Unlike `cqp report`, these fail loudly: a bad policy file should fail its MR.
"""

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
) -> int:
    """`cqp waiver`: asks CQP to accept a finding for a number of days."""
    return create(
        "/v1/waivers",
        {"service": service, "rule": rule, "reason": reason, "ticket": ticket, "days": days},
        ("waiver_id", "rule", "ticket", "expires_at"),
        url,
        token,
        client,
    )
