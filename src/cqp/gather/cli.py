"""The `cqp` command. `cqp report` uploads a job's reports and never fails the job."""

import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path

import httpx

STATE = Path(".cqp")
RETRY_DELAYS = (0, 2, 4, 8)  # about 15s in all; a report must never hold the pipeline up


def warn(msg: str) -> int:
    """Prints to the CI job log and returns 0."""
    # Always 0: a reporting problem must never fail the team's job.
    print(f"cqp: {msg}", file=sys.stderr)
    return 0


def report(
    service: str,
    check: str,
    fmt: str,
    pattern: str,
    url: str,
    token: str,
    client: httpx.Client,
    sleep=time.sleep,
) -> int:
    """`cqp report`: uploads the files matching a pattern, with retries."""
    paths = sorted(glob.glob(pattern, recursive=True))
    if not paths:
        return warn(f"no files match {pattern!r}; nothing reported")

    image_file = STATE / "image-digest"  # written by the build job
    meta = {
        "service": service,
        "check": check,
        "format": fmt,
        "image": image_file.read_text().strip() if image_file.exists() else None,
    }

    for delay in RETRY_DELAYS:
        sleep(delay)
        try:
            files = [("files", (Path(p).name, Path(p).read_bytes())) for p in paths]
            r = client.post(
                f"{url}/v1/reports",
                data={"meta": json.dumps(meta)},
                files=files,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
        except httpx.HTTPError:
            continue
        if r.status_code == 202:
            # One file per job: CI merges every job's .cqp/ into the gate job, which sends these.
            events = STATE / "events"
            events.mkdir(parents=True, exist_ok=True)
            with open(events / f"{os.environ.get('CI_JOB_ID', 'local')}.txt", "a") as f:
                f.write(r.json()["event_id"] + "\n")
            return 0
        if r.status_code < 500:  # our request is wrong; retrying won't help
            return warn(f"report rejected ({r.status_code}): {r.text}")

    return warn("CQP unreachable; reports stay in CI artifacts for backfill")


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `cqp` command."""
    parser = argparse.ArgumentParser(prog="cqp")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report", help="upload this job's reports to CQP")
    rep.add_argument("--service", default=os.environ.get("CI_PROJECT_NAME"))
    rep.add_argument("--check", required=True)
    rep.add_argument("--format", required=True)
    rep.add_argument("--files", required=True, help="glob, e.g. 'reports/**/*.xml'")
    gat = sub.add_parser("gate", help="ask CQP whether this change may move on")
    gat.add_argument("--service", default=os.environ.get("CI_PROJECT_NAME"))
    gat.add_argument(
        "--stage", required=True, choices=["pr", "build", "integration", "prod-deploy"]
    )
    gat.add_argument("--max-wait", type=float, default=120, help="seconds to wait while pending")
    gat.add_argument(
        "--fix", metavar="TICKET", help="this change is a fix for TICKET (allowed during incidents)"
    )
    pol = sub.add_parser("policy", help="check or publish the team policy file")
    pol.add_argument("action", choices=["check", "publish"])
    pol.add_argument("--service", default=os.environ.get("CI_PROJECT_NAME"))
    pol.add_argument("--file", default=".quality/policy.yaml")
    wai = sub.add_parser("waiver", help="accept a known finding for a while (approvers only)")
    wai.add_argument("--service", required=True)
    wai.add_argument("--rule", required=True, help="e.g. baseline.scan.sca")
    wai.add_argument("--reason", required=True)
    wai.add_argument("--ticket", required=True)
    wai.add_argument("--days", type=int, default=14)
    kil = sub.add_parser(
        "kill-switch", help="switch a misbehaving rule off everywhere (platform team)"
    )
    kil.add_argument("--rule", required=True)
    kil.add_argument("--reason", required=True)
    kil.add_argument("--hours", type=int, default=4)
    brk = sub.add_parser(
        "break-glass", help="emergency: let one change through one stage (reviewed after)"
    )
    brk.add_argument("--decision", required=True, help="the decision ID from the gate's output")
    brk.add_argument("--reason", required=True)
    brk.add_argument("--ticket", required=True, help="the incident or change ticket")
    args = parser.parse_args(argv)

    url, token = os.environ.get("CQP_URL"), os.environ.get("CQP_TOKEN")
    if args.command in ("policy", "waiver", "kill-switch", "break-glass"):
        from cqp.decide import team_cli

        # kill-switch and break-glass aren't about one service, so they have no --service.
        if not (url and token and getattr(args, "service", True)):
            print("cqp: CQP_URL, CQP_TOKEN or service missing", file=sys.stderr)
            return 1
        with httpx.Client() as client:
            if args.command == "policy":
                return team_cli.policy(args.action, args.service, args.file, url, token, client)
            if args.command == "waiver":
                return team_cli.waiver(
                    args.service, args.rule, args.reason, args.ticket, args.days, url, token, client
                )
            if args.command == "kill-switch":
                return team_cli.create(
                    "/v1/kill-switches",
                    {"rule": args.rule, "reason": args.reason, "hours": args.hours},
                    ("kill_id", "rule", "expires_at"),
                    url,
                    token,
                    client,
                )
            return team_cli.create(
                f"/v1/decisions/{args.decision}/break-glass",
                {"reason": args.reason, "ticket": args.ticket},
                ("break_glass_id", "service", "stage"),
                url,
                token,
                client,
            )
    if args.command == "gate":
        from cqp.decide.gate import fallback, gate

        if not (url and token and args.service):
            return fallback(args.stage, "CQP_URL, CQP_TOKEN or service missing")
        with httpx.Client() as client:
            return gate(
                args.service, args.stage, url, token, client, args.max_wait, fix_ticket=args.fix
            )

    if not (url and token and args.service):
        return warn("CQP_URL, CQP_TOKEN or service missing; nothing reported")
    with httpx.Client() as client:
        return report(args.service, args.check, args.format, args.files, url, token, client)
