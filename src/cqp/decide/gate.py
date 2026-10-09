"""`cqp gate`: the golden-path CI step.

Asks CQP for a decision and turns it into an exit code.
"""

import os
import sys
import time
from pathlib import Path

import httpx

STATE = Path(".cqp")
# A warning doesn't stop the pipeline; it shows in the job log and the MR comment.
EXIT = {"PROCEED": 0, "PROCEED_WITH_WARNING": 0, "BLOCKED": 1, "REQUIRE_VALIDATION": 2}
POLL_SECONDS = 5


def say(msg: str) -> None:
    """Prints to the CI job log."""
    print(f"cqp: {msg}", file=sys.stderr)


def fallback(stage: str, reason: str) -> int:
    """What the gate does when CQP can't be reached."""
    # Fail-mode matrix: carry on with a warning, except production, which needs an approval.
    say(
        f"CQP unavailable ({reason}); "
        f"{'approval needed' if stage == 'prod-deploy' else 'continuing'}"
    )
    return EXIT["REQUIRE_VALIDATION"] if stage == "prod-deploy" else 0


def ask(client, url, token, body, sleep) -> dict:
    """Asks CQP for a decision, retrying server errors twice."""
    for attempt in range(3):
        try:
            r = client.post(
                f"{url}/v1/decisions",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            # 4xx: CQP said no, and asking again won't change that. 5xx: try up to 3 times.
            if e.response.status_code < 500 or attempt == 2:
                raise
        except httpx.HTTPError:
            if attempt == 2:
                raise
        sleep(1)
    raise AssertionError("unreachable: the last attempt returns or raises")


def gate(
    service: str,
    stage: str,
    url: str,
    token: str,
    client: httpx.Client,
    max_wait: float = 120,
    sleep=time.sleep,
    clock=time.monotonic,
    fix_ticket: str | None = None,
) -> int:
    """Asks until the answer is final (or max_wait runs out) and returns the job's exit code."""
    image_file = STATE / "image-digest"
    body = {
        "service": service,
        "stage": stage,
        # Written by the build job. Without it the decision is about the commit.
        "image": image_file.read_text().strip() if image_file.exists() else None,
        # Every job's uploads, so CQP doesn't decide before they have landed.
        "event_ids": [e for f in sorted(STATE.glob("events/*.txt")) for e in f.read_text().split()],
        "merge_request": int(os.environ["CI_MERGE_REQUEST_IID"])
        if os.environ.get("CI_MERGE_REQUEST_IID")
        else None,
        "fix_ticket": fix_ticket,
    }

    # Poll while the answer is pending, but not forever: for long checks the notifier
    # re-runs this job once the result lands, so there's no need to hold a runner here.
    give_up = clock() + max_wait
    while True:
        try:
            d = ask(client, url, token, body, sleep)
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                # Refused (bad token, wrong repo...): a setup problem,
                # so fail loudly, never fall back.
                say(f"CQP refused the request ({e.response.status_code}): {e.response.text}")
                return EXIT["BLOCKED"]
            return fallback(stage, str(e))
        except httpx.HTTPError as e:
            return fallback(stage, str(e))
        # Outside enforce mode the answer can't stop anything, so there's nothing to wait for.
        if d["status"] == "final" or clock() >= give_up or d.get("mode", "enforce") != "enforce":
            break
        sleep(POLL_SECONDS)

    say(f"{d['outcome']} at {stage} · policy {d['policy_version']} · decision {d['decision_id']}")
    for f in d["findings"]:
        say(f"  [{f['effect']}] {f['message']}")
    for w in d["waiting"]:
        say(f"  waiting for {w.get('check') or w.get('event_id')}")
    mode = d.get("mode", "enforce")
    if mode != "enforce":
        # Adoption phase: show what would happen, but never stop the pipeline.
        if EXIT[d["outcome"]] != 0 or d["status"] == "pending":
            say(f"{mode} mode: this would be {d['outcome']} once enforced; carrying on")
        return 0
    return EXIT[d["outcome"]]
