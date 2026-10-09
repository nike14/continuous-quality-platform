"""Webhook adapters: one function per tool, turning its payload into raw events."""

import hashlib
import hmac

from cqp.gather.event import raw_event


def valid_signature(secret: str, body: bytes, header: str | None) -> bool:
    """True if the body was signed with this tool's secret."""
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # compare_digest takes the same time however close the guess is.
    return header is not None and hmac.compare_digest(expected, header)


def pact_broker(body: dict) -> list[dict]:
    """'Contract verification published'.

    The body shape is set by our Pact Broker webhook template.
    """
    return [
        raw_event(
            producer="adapter:pact-broker",
            service=body["provider"],
            check="tests.contract",
            fmt="pact",
            # Pact's provider version is the commit the provider's CI published it with.
            commit=body["provider_version"],
            # Each verification has its own URL, so it doubles as a unique run ID.
            run=body["verification_url"],
            payload={"status": "pass" if body["success"] else "fail", "consumer": body["consumer"]},
        )
    ]


def argo_rollouts(body: dict) -> list[dict]:
    """Canary analysis result, sent by our Argo Rollouts notification template.

    The rollout controller has already rolled back on a failure; CQP only records it.
    """
    return [
        raw_event(
            producer="adapter:argo-rollouts",
            service=body["service"],
            check="deploy.canary",
            fmt="argo",
            commit=body["commit"],
            run=body["analysis_run"],
            image=body["image"],
            # Only a successful analysis passes;
            # Failed, Error or Inconclusive all count as a failed canary.
            payload={
                "status": "pass" if body["phase"] == "Successful" else "fail",
                "phase": body["phase"],
            },
        )
    ]


RUNTIME = "runtime"  # commit for service-level state that isn't tied to one change


def runtime(body: dict) -> list[dict]:
    """Production state from monitoring and incident tools, sent by their webhook templates.

    {"type": "slo", "service", "error_budget_remaining", "at"}
    {"type": "incident", "service", "id", "severity", "state": "open|resolved"}
    {"type": "freeze", "active", "at"}  (organisation-wide, service "*")
    """
    kind = body["type"]
    if kind == "slo":
        check, run = "runtime.slo", body["at"]
        payload = {
            "status": "pass" if body["error_budget_remaining"] > 0 else "fail",
            "error_budget_remaining": body["error_budget_remaining"],
        }
    elif kind == "incident":
        # One event per state change, so "resolved" isn't dropped as a duplicate of "open".
        check, run = "runtime.incident", f"{body['id']}:{body['state']}"
        payload = {
            "status": "fail" if body["state"] == "open" else "pass",
            "id": body["id"],
            "severity": body["severity"],
            "state": body["state"],
        }
    elif kind == "freeze":
        check, run = "org.freeze", body["at"]
        payload = {"status": "fail" if body["active"] else "pass", "active": body["active"]}
    else:
        raise ValueError(f"unknown runtime event type {kind!r}")
    return [
        raw_event(
            producer="adapter:runtime",
            service=body.get("service", "*"),
            check=check,
            fmt="runtime",
            commit=RUNTIME,
            run=run,
            payload=payload,
        )
    ]


ADAPTERS = {"pact-broker": pact_broker, "argo-rollouts": argo_rollouts, "runtime": runtime}
