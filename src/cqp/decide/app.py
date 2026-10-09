"""Decision API: the only thing pipelines call.

Check completeness, apply policy, record the result.
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from fastapi import FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, ValidationError

from cqp.decide import db, portal
from cqp.decide.engine import completeness, outcome
from cqp.decide.policy import Policy
from cqp.decide.team import TeamPolicy
from cqp.metrics import (
    BREAK_GLASS,
    DECISION_ERRORS,
    DECISION_SECONDS,
    DECISIONS,
    FINDINGS,
    KILL_SWITCHES,
    PUBLISH_FAILURES,
    metrics_response,
)
from cqp.store.db import latest

# How long a caller's own uploads may still be in flight before we decide without them.
IN_FLIGHT_GRACE = timedelta(seconds=60)
DECISIONS_TOPIC = "cqp.decisions"
BACKTEST_DECISIONS = 40  # enough history to be telling, quick enough for an MR check
APPROVER_ROLES = {"approver", "security"}
log = logging.getLogger(__name__)


class DecisionRequest(BaseModel):
    """What `cqp gate` sends. Who is asking comes from the token, not from here."""

    service: str
    stage: Literal["pr", "build", "integration", "prod-deploy"]
    image: str | None = None
    event_ids: list[str] = []
    merge_request: int | None = None  # only used to know where to post the MR comment
    # A fix may go out during an incident or with no error budget left. It must name its
    # ticket, which is recorded and shown, so claiming "fix" is never silent.
    fix_ticket: str | None = Field(default=None, min_length=1)


class WaiverRequest(BaseModel):
    """A request to accept one finding on one service for a number of days."""

    service: str
    rule: str
    reason: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    days: int = Field(ge=1)


class KillSwitchRequest(BaseModel):
    """A request to switch one rule off everywhere for a few hours."""

    rule: str
    reason: str = Field(min_length=1)
    hours: int = Field(ge=1)


class BreakGlassRequest(BaseModel):
    """An emergency override for the change and stage of one decision."""

    reason: str = Field(min_length=1)
    ticket: str = Field(min_length=1)  # the incident or change ticket the review hangs off


def create_app(
    connect: Callable,
    policy: Policy,
    verify_token: Callable[[str], dict],
    publish: Callable[[str, str, dict], None],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    """Builds the Decision API.

    Storage, policy, token check and Kafka are passed in, so tests can swap them.
    """
    app = FastAPI(title="CQP decisions")

    def verify(authorization: str) -> dict:
        """Checks the bearer token and returns its claims, or answers 401."""
        try:
            return verify_token(authorization.removeprefix("Bearer "))
        except jwt.PyJWTError as e:
            raise HTTPException(401, "invalid token") from e

    @app.get("/metrics")
    def metrics():
        """Prometheus scrape endpoint."""
        return metrics_response()

    @app.post("/v1/decisions")
    def decide(req: DecisionRequest, authorization: str = Header(...)):
        """POST /v1/decisions: times every request and counts our own failures."""
        with DECISION_SECONDS.time():
            try:
                return make_decision(req, authorization)
            except HTTPException:
                raise  # the caller's mistake, not ours
            except Exception:
                DECISION_ERRORS.inc()
                raise

    def make_decision(req: DecisionRequest, authorization: str) -> dict:
        """Who's asking, what evidence is in, what the rules say.

        Then record the decision and tell the notifier.
        """
        claims = verify(authorization)
        if req.stage == "prod-deploy":
            # Production is asked about by the deploy system, about an image, never by the
            # service's own pipeline (which could otherwise deploy around its own gates).
            if not policy.catalog.is_deployer(claims):
                raise HTTPException(403, "only the deploy system may ask for prod-deploy decisions")
            if not req.image:
                raise HTTPException(422, "a prod-deploy decision needs the image being deployed")
        elif not policy.catalog.owns(claims.get("project_path"), req.service):
            raise HTTPException(403, f"{claims.get('project_path')} doesn't own {req.service}")
        # The deployer's own commit says nothing about the image; its evidence comes via builds.
        commit = None if req.stage == "prod-deploy" else claims.get("sha")
        # A decision is about the image once there is one, before that about the commit.
        subject = req.image or commit
        if not subject:
            # Only a token without a commit claim gets here; there is nothing to decide about.
            raise HTTPException(422, "the token has no commit (sha) and no image was given")
        tier = policy.tier(req.service)
        now = clock()

        # One transaction: the decision is recorded against exactly the evidence we read.
        with connect() as conn, conn.transaction():
            first = db.first_asked(conn, req.service, subject, req.stage) or now
            evidence = latest(conn, req.service, req.image, commit=commit)
            # A result past its max age (a scan from last month) doesn't count; it's missing.
            stale = {
                check
                for check, e in evidence.items()
                if (age := policy.max_age(check))
                and now - datetime.fromisoformat(e["observed_at"]) > age
            }
            evidence = {c: e for c, e in evidence.items() if c not in stale}
            # The caller's own uploads may still be on their way through Kafka. Wait for them
            # for the first minute only; after that a lost upload is missing, not a reason to wait.
            in_flight = [] if now - first > IN_FLIGHT_GRACE else db.not_stored(conn, req.event_ids)
            team_version, team = db.team_policy(conn, req.service)
            # A team can wait for more checks, but the baseline's expectations always win.
            expected = {
                **team.get("extra_checks", {}).get(req.stage, {}),
                **policy.expected(req.stage, tier),
            }
            waiting, missing = completeness(expected, evidence, first, now)
            missing = [{**m, "stale": True} if m["check"] in stale else m for m in missing]
            glass = db.break_glass(conn, req.service, subject, req.stage, now)
            if glass:
                # Under break-glass nobody waits: whatever hasn't reported counts as missing now.
                missing += [
                    {"check": w["check"], "on_missing": expected[w["check"]]["on_missing"]}
                    for w in waiting
                ]
                waiting, in_flight = [], []
            kill_switches = db.active_kill_switches(conn, now)
            policy_input = {
                "service": req.service,
                "stage": req.stage,
                "tier": tier,
                "evidence": evidence,
                "missing": missing,
                "team": team,
                "team_policy_version": team_version,
                "waivers": db.active_waivers(conn, req.service, now),
                "kill_switches": kill_switches,
                "break_glass": glass,
            }
            if req.stage == "prod-deploy":
                # Production state is part of the input, so a replay sees exactly what we saw.
                policy_input["runtime"] = db.runtime(conn, req.service, now)
                policy_input["fix"] = req.fix_ticket is not None
                policy_input["fix_ticket"] = req.fix_ticket

            # Never decide on half the evidence: report what we're waiting for instead.
            # A pending answer still says REQUIRE_VALIDATION,
            # so a gate that stops polling won't proceed.
            pending = bool(in_flight or waiting)
            all_waiting = waiting + [{"event_id": e} for e in in_flight]
            job_id = str(claims["job_id"]) if claims.get("job_id") is not None else None
            if pending:
                # The gate polls every few seconds. If this job is still waiting on exactly the
                # same things, hand back the same decision: no new row, MR edit or status post.
                prev = db.latest_decision(conn, req.service, subject, req.stage)
                if (
                    prev is not None
                    and prev["status"] == "pending"
                    and prev["job_id"] == job_id
                    and prev["waiting"] == all_waiting
                    and prev["policy_version"] == policy.version
                ):
                    return prev
            findings = [] if pending else policy.evaluate(policy_input)
            decision = {
                "decision_id": uuid.uuid4().hex,
                "service": req.service,
                "subject": subject,
                "stage": req.stage,
                "status": "pending" if pending else "final",
                "outcome": "REQUIRE_VALIDATION" if pending else outcome(findings),
                "findings": findings,
                "waiting": all_waiting,
                "input": policy_input,
                "policy_version": policy.version,
                "created_at": now,
                "job_id": job_id,
                # The outcome is the same in every mode; only what the gate does with it differs.
                "mode": policy.catalog.mode(req.service),
            }
            db.insert(conn, decision)

        # Counted after the commit, so a rolled-back transaction never shows up as a decision.
        DECISIONS.labels(req.stage, decision["status"], decision["outcome"]).inc()
        KILL_SWITCHES.set(len(kill_switches))
        for f in findings:
            FINDINGS.labels(f["rule"], f["effect"]).inc()

        # Routing for the notifier. Project, pipeline and job come from the token, so a caller
        # can only get comments posted or jobs retried in its own project.
        context = {
            "project": claims.get("project_path"),
            "pipeline_id": claims.get("pipeline_id"),
            "job_id": claims.get("job_id"),
            "commit": commit,
            "merge_request": req.merge_request,
        }
        try:
            event = jsonable_encoder({**decision, "context": context})
            publish(DECISIONS_TOPIC, f"{req.service}:{subject}", event)
        except Exception:
            # Notifications may lag; the decision itself is already recorded and returned.
            PUBLISH_FAILURES.inc()
            log.warning("could not publish decision %s", decision["decision_id"], exc_info=True)
        return decision

    @app.put("/v1/team-policies/{service}")
    def put_team_policy(
        service: str, body: dict, dry_run: bool = False, authorization: str = Header(...)
    ):
        """Validates a team policy, backtests it, and publishes it unless it's a dry run."""
        claims = verify(authorization)
        # Only the service's own repo may set its policy, and only from a protected branch,
        # so a change has been through review. A dry run (the MR check) needs neither.
        if not policy.catalog.owns(claims.get("project_path"), service):
            raise HTTPException(403, f"{claims.get('project_path')} doesn't own {service}")
        if not dry_run and str(claims.get("ref_protected")).lower() != "true":
            raise HTTPException(403, "publish team policy from a protected branch")
        try:
            # exclude_defaults: a left-out section and an empty one give the same version.
            team = TeamPolicy.model_validate(body).model_dump(exclude_defaults=True)
        except ValidationError as e:
            raise HTTPException(
                422, {"errors": [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]}
            ) from e
        if errors := policy.team_errors(team):
            raise HTTPException(422, {"errors": errors})
        # Same content, same version, so republishing an unchanged file is harmless.
        version = hashlib.sha256(json.dumps(team, sort_keys=True).encode()).hexdigest()[:12]

        with connect() as conn, conn.transaction():
            result = {"version": version, "backtest": backtest(conn, policy, service, team)}
            if not dry_run:
                db.publish_team_policy(
                    conn,
                    service,
                    version,
                    team,
                    claims.get("user_login") or claims["project_path"],
                    clock(),
                )
        return result

    @app.get("/v1/team-policies/{service}")
    def get_team_policy(service: str):
        """The service's current team policy and its version."""
        with connect() as conn:
            version, team = db.team_policy(conn, service)
        return {"service": service, "version": version, "policy": team}

    @app.post("/v1/waivers", status_code=201)
    def create_waiver(req: WaiverRequest, authorization: str = Header(...)):
        """Creates a waiver if the rules in baseline.rego allow it for this person."""
        claims = verify(authorization)
        if errors := policy.waiver_errors(req.model_dump(), claims.get("cqp_role")):
            raise HTTPException(403, {"errors": errors})
        now = clock()
        # The approver is whoever's token this is; a request can't name someone else.
        waiver = {
            **req.model_dump(exclude={"days"}),
            "waiver_id": uuid.uuid4().hex,
            "approver": claims.get("user_login") or "unknown",
            "created_at": now,
            "expires_at": now + timedelta(days=req.days),
        }
        with connect() as conn, conn.transaction():
            db.insert_waiver(conn, waiver)
        return waiver

    @app.get("/v1/waivers")
    def list_waivers(service: str):
        """Active waivers for a service."""
        with connect() as conn:
            return db.active_waivers(conn, service, clock())

    @app.delete("/v1/waivers/{waiver_id}", status_code=204)
    def revoke(waiver_id: str, authorization: str = Header(...)):
        """Ends a waiver early. Approvers only."""
        # The same people who can create a waiver can end one.
        if verify(authorization).get("cqp_role") not in APPROVER_ROLES:
            raise HTTPException(403, "only approvers can revoke waivers")
        with connect() as conn, conn.transaction():
            # Revoked, not deleted: past decisions still point at it.
            if not db.revoke_waiver(conn, waiver_id, clock()):
                raise HTTPException(404, "no such active waiver")

    @app.post("/v1/kill-switches", status_code=201)
    def kill(req: KillSwitchRequest, authorization: str = Header(...)):
        """Switches a rule off everywhere. Platform team only."""
        claims = verify(authorization)
        # Who may do this, and for how long, is decided in baseline.rego.
        if errors := policy.kill_errors(req.model_dump(), claims.get("cqp_role")):
            raise HTTPException(403, {"errors": errors})
        now = clock()
        k = {
            "kill_id": uuid.uuid4().hex,
            "rule": req.rule,
            "reason": req.reason,
            "created_by": claims.get("user_login") or "unknown",
            "created_at": now,
            "expires_at": now + timedelta(hours=req.hours),
        }
        with connect() as conn, conn.transaction():
            db.insert_kill_switch(conn, k)
        # Logged loudly: a rule switched off affects every service.
        log.warning("rule %s switched off by %s: %s", req.rule, k["created_by"], req.reason)
        return k

    @app.get("/v1/kill-switches")
    def list_kill_switches():
        """Rules currently switched off."""
        with connect() as conn:
            return db.active_kill_switches(conn, clock())

    @app.delete("/v1/kill-switches/{kill_id}", status_code=204)
    def clear(kill_id: str, authorization: str = Header(...)):
        """Switches a rule back on. Platform team only."""
        # Only the platform team switches rules back on, as they switched them off.
        if verify(authorization).get("cqp_role") != "platform":
            raise HTTPException(403, "only the platform team can switch rules back on")
        with connect() as conn, conn.transaction():
            # Cleared, not deleted, so the history shows when the rule was off.
            if not db.clear_kill_switch(conn, kill_id, clock()):
                raise HTTPException(404, "no such active kill switch")

    @app.post("/v1/decisions/{decision_id}/break-glass", status_code=201)
    def break_glass(decision_id: str, req: BreakGlassRequest, authorization: str = Header(...)):
        """Lets this change through this stage for 24h. Re-run the gate job to pick it up."""
        claims = verify(authorization)
        if errors := policy.break_glass_errors(claims.get("cqp_role")):
            raise HTTPException(403, {"errors": errors})
        with connect() as conn, conn.transaction():
            d = db.get(conn, decision_id)
            if d is None:
                raise HTTPException(404, "no such decision")
            # Tied to that decision's change and stage, so it can't be reused for another commit.
            b = {
                "break_glass_id": uuid.uuid4().hex,
                "decision_id": decision_id,
                "service": d["service"],
                "subject": d["subject"],
                "stage": d["stage"],
                "reason": req.reason,
                "ticket": req.ticket,
                "created_by": claims.get("user_login") or "unknown",
                "created_at": clock(),
            }
            db.insert_break_glass(conn, b)
        BREAK_GLASS.labels(d["stage"]).inc()
        log.warning(
            "break-glass on %s %s at %s by %s: %s",
            d["service"],
            d["subject"],
            d["stage"],
            b["created_by"],
            req.reason,
        )
        return b

    # Read-only and no token needed, like the GET endpoints; changes go through the checked ones.
    @app.get("/portal", response_class=HTMLResponse)
    def portal_page():
        with connect() as conn:
            view = db.portal_view(conn, clock())
        return portal.render(view, policy.catalog.services)

    @app.get("/v1/decisions/{decision_id}")
    def get_decision(decision_id: str):
        """One stored decision, including the input it was made on."""
        # The record includes the input it was decided on, so callers can see why.
        with connect() as conn:
            d = db.get(conn, decision_id)
        if d is None:
            raise HTTPException(404, "no such decision")
        return d

    return app


def backtest(conn, policy: Policy, service: str, team: dict) -> dict:
    """How recent decisions would change under a new team policy (same evidence, same waivers).

    extra_checks aren't backtested: they change what we wait for, not how evidence is judged.
    """
    changed = []
    recent = db.recent_final(conn, service, BACKTEST_DECISIONS)
    for d in recent:
        # Re-run the rules on the stored input, with only the team policy swapped.
        now_outcome = outcome(policy.evaluate({**d["input"], "team": team}))
        if now_outcome != d["outcome"]:
            changed.append(
                {
                    "decision_id": d["decision_id"],
                    "stage": d["stage"],
                    "was": d["outcome"],
                    "would_be": now_outcome,
                }
            )
    # The count shows how big the change is; five examples are enough for an MR log.
    return {"decisions": len(recent), "changed": len(changed), "examples": changed[:5]}


def replay(connect: Callable, policy: Policy, decision_id: str) -> bool:
    """Re-evaluates a stored decision on its own snapshot and rules. True if the outcome matches."""
    with connect() as conn:
        d = db.get(conn, decision_id)
        if d is None:
            raise LookupError(f"no decision {decision_id}")
        version = d["policy_version"]
        # Older decisions are replayed on the rules they were made with, not today's.
        rego = "" if version == policy.version else db.policy_rego(conn, version)
    if rego is None:
        raise ValueError(f"decision used policy {version}, which was never stored")
    # A pending decision never ran the rules, so there is nothing to compare.
    if d["status"] == "pending":
        return True
    return outcome(policy.evaluate(d["input"], version=version, rego=rego)) == d["outcome"]
