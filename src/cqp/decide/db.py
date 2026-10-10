"""Decision records. Insert-only: a later decision supersedes an earlier one, nothing is edited."""

from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA = """
-- subject is the image digest, or the commit before there is an image.
-- input is the exact policy input, so any decision can be replayed later.
CREATE TABLE IF NOT EXISTS decisions (
    decision_id    text PRIMARY KEY,
    service        text NOT NULL,
    subject        text NOT NULL,
    stage          text NOT NULL,
    status         text NOT NULL,
    outcome        text NOT NULL,
    findings       jsonb NOT NULL,
    waiting        jsonb NOT NULL,
    input          jsonb NOT NULL,
    policy_version text NOT NULL,
    created_at     timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_by_subject ON decisions (service, subject, stage, created_at);
-- The CI job that asked, so a repeat poll from the same job can reuse its pending decision.
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS job_id text;
-- The service's adoption mode when it was decided: shadow, advisory or enforce.
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS mode text;

-- Every baseline the Decision API has run with,
-- so an old decision can be replayed on its own rules.
CREATE TABLE IF NOT EXISTS policy_versions (
    version  text PRIMARY KEY,
    rego     text NOT NULL,
    saved_at timestamptz NOT NULL DEFAULT now()
);

-- Every published version is kept, so a decision's team_policy_version can be looked up later.
CREATE TABLE IF NOT EXISTS team_policies (
    service      text NOT NULL,
    version      text NOT NULL,
    policy       jsonb NOT NULL,
    published_by text NOT NULL,
    published_at timestamptz NOT NULL,
    PRIMARY KEY (service, version)
);

CREATE TABLE IF NOT EXISTS waivers (
    waiver_id  text PRIMARY KEY,
    service    text NOT NULL,
    rule       text NOT NULL,
    reason     text NOT NULL,
    ticket     text NOT NULL,
    approver   text NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);
CREATE INDEX IF NOT EXISTS waivers_by_service ON waivers (service, expires_at);
-- Added after the table; IF NOT EXISTS keeps existing local databases working.
ALTER TABLE waivers ADD COLUMN IF NOT EXISTS reminded_at timestamptz;
-- What exactly is accepted (CVE IDs, fingerprints); empty means the whole rule.
ALTER TABLE waivers ADD COLUMN IF NOT EXISTS items jsonb NOT NULL DEFAULT '[]';

-- Platform team switches off a misbehaving rule everywhere, for a few hours.
CREATE TABLE IF NOT EXISTS kill_switches (
    kill_id    text PRIMARY KEY,
    rule       text NOT NULL,
    reason     text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    cleared_at timestamptz
);

-- An emergency override for one change at one stage. Always reviewed afterwards.
CREATE TABLE IF NOT EXISTS break_glass (
    break_glass_id text PRIMARY KEY,
    decision_id    text NOT NULL,
    service        text NOT NULL,
    subject        text NOT NULL,
    stage          text NOT NULL,
    reason         text NOT NULL,
    ticket         text NOT NULL,
    created_by     text NOT NULL,
    created_at     timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS break_glass_by_subject
    ON break_glass (service, subject, stage, created_at);
"""


def migrate(conn: psycopg.Connection) -> None:
    """Creates the tables if they don't exist yet."""
    conn.execute(SCHEMA)
    conn.commit()


def first_asked(conn, service: str, subject: str, stage: str):
    """When this change was first asked about at this stage; deadlines count from here."""
    return conn.execute(
        "SELECT min(created_at) FROM decisions WHERE service = %s AND subject = %s "
        "AND stage = %s",
        (service, subject, stage),
    ).fetchone()[0]


def not_stored(conn, event_ids: list[str]) -> list[str]:
    """Which of the caller's own uploads haven't reached the store yet."""
    stored = {
        r[0]
        for r in conn.execute("SELECT event_id FROM signals WHERE event_id = ANY(%s)", (event_ids,))
    }
    return [e for e in event_ids if e not in stored]


def insert(conn, d: dict) -> None:
    """Stores a decision. Decisions are never updated."""
    # The full input is stored with it; that's what makes replay possible.
    conn.execute(
        """INSERT INTO decisions (decision_id, service, subject, stage, status, outcome, findings,
                                  waiting, input, policy_version, created_at, job_id, mode)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            d["decision_id"],
            d["service"],
            d["subject"],
            d["stage"],
            d["status"],
            d["outcome"],
            Jsonb(d["findings"]),
            Jsonb(d["waiting"]),
            Jsonb(d["input"]),
            d["policy_version"],
            d["created_at"],
            d.get("job_id"),
            d.get("mode", "enforce"),
        ),
    )


def save_policy_version(conn, version: str, rego: str) -> None:
    """Keeps this baseline's Rego. Saving the same version again changes nothing."""
    conn.execute(
        "INSERT INTO policy_versions (version, rego) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (version, rego),
    )


def policy_rego(conn, version: str) -> str | None:
    """The Rego a stored baseline version ran with, or None if it was never saved."""
    row = conn.execute("SELECT rego FROM policy_versions WHERE version = %s", (version,)).fetchone()
    return row[0] if row else None


def latest_decision(conn, service: str, subject: str, stage: str) -> dict | None:
    """The newest decision for this change and stage, or None."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """SELECT * FROM decisions WHERE service = %s AND subject = %s AND stage = %s
               ORDER BY created_at DESC LIMIT 1""",
            (service, subject, stage),
        ).fetchone()


def get(conn, decision_id: str) -> dict | None:
    """One decision by ID, or None."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT * FROM decisions WHERE decision_id = %s", (decision_id,)
        ).fetchone()


HEALTH_FRESHNESS = timedelta(minutes=5)


def runtime(conn, service: str, now: datetime) -> dict:
    """Production state for a service, read from the stored runtime signals (newest of each)."""
    # Service "*" holds organisation-wide state, like a change freeze.
    rows = conn.execute(
        """SELECT DISTINCT ON (service, check_name) service, check_name, payload, observed_at
           FROM signals
           WHERE service IN (%s, '*') AND check_name IN ('runtime.slo', 'org.freeze')
           ORDER BY service, check_name, observed_at DESC""",
        (service,),
    ).fetchall()
    newest = {(s, c): (p, t) for s, c, p, t in rows}
    slo, freeze = newest.get((service, "runtime.slo")), newest.get(("*", "org.freeze"))
    # Each incident's own latest state, so resolving one doesn't hide another that is still open.
    # On a tie, resolved wins: an incident doesn't reopen under the same event time.
    open_severities = [
        r[0]
        for r in conn.execute(
            """SELECT payload->>'severity' FROM (
                 SELECT DISTINCT ON (payload->>'id') payload
                 FROM signals WHERE service = %s AND check_name = 'runtime.incident'
                 ORDER BY payload->>'id', observed_at DESC, payload->>'state' = 'resolved' DESC
               ) newest_per_incident
               WHERE payload->>'state' = 'open'""",
            (service,),
        )
    ]
    # Any failed canary for this service in the last day means the next deploy should go slower.
    rolled_back = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM signals WHERE service = %s AND check_name = 'deploy.canary' "
        "AND status = 'fail' AND observed_at > %s)",
        (service, now - timedelta(hours=24)),
    ).fetchone()[0]
    # Missing data stays None rather than a guess; baseline.rego decides what None means.
    return {
        "error_budget_remaining": slo[0]["error_budget_remaining"] if slo is not None else None,
        "health_observed_at": slo[1].isoformat() if slo is not None else None,
        "health_fresh": slo is not None and now - slo[1] <= HEALTH_FRESHNESS,
        # The most severe open one (sev1 sorts before sev2), or None.
        "incident": min(open_severities) if open_severities else None,
        "rolled_back_recently": rolled_back,
        "freeze": bool(freeze and freeze[0]["active"]),
    }


def publish_team_policy(
    conn, service: str, version: str, body: dict, by: str, now: datetime
) -> None:
    """Stores a team policy version and makes it the current one."""
    # Republishing an older version makes it current again.
    conn.execute(
        """INSERT INTO team_policies (service, version, policy, published_by, published_at)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (service, version) DO UPDATE
           SET published_by = EXCLUDED.published_by, published_at = EXCLUDED.published_at""",
        (service, version, Jsonb(body), by, now),
    )


def team_policy(conn, service: str) -> tuple[str | None, dict]:
    """The current team policy for a service: (version, body), or (None, {}) if it has none."""
    row = conn.execute(
        "SELECT version, policy FROM team_policies WHERE service = %s "
        "ORDER BY published_at DESC LIMIT 1",
        (service,),
    ).fetchone()
    return (row[0], row[1]) if row else (None, {})


def insert_waiver(conn, w: dict) -> None:
    """Stores a new waiver."""
    # Expiry is fixed when it's created; to go longer, someone makes a new waiver.
    conn.execute(
        """INSERT INTO waivers (waiver_id, service, rule, reason, ticket, approver,
                                created_at, expires_at, items)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            w["waiver_id"],
            w["service"],
            w["rule"],
            w["reason"],
            w["ticket"],
            w["approver"],
            w["created_at"],
            w["expires_at"],
            Jsonb(w.get("items", [])),
        ),
    )


def active_waivers(conn, service: str, now: datetime) -> list[dict]:
    """Waivers for a service that haven't expired or been revoked."""
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """SELECT waiver_id, rule, ticket, reason, approver, expires_at, items FROM waivers
               WHERE service = %s AND expires_at > %s AND revoked_at IS NULL
               ORDER BY created_at""",
            (service, now),
        ).fetchall()
    # Dates as text: this goes into the policy input, which is stored as plain JSON for replay.
    return [{**r, "expires_at": r["expires_at"].isoformat()} for r in rows]


def waiver_service(conn, waiver_id: str) -> str | None:
    """The service an active waiver is for, or None if there is no such active waiver."""
    row = conn.execute(
        "SELECT service FROM waivers WHERE waiver_id = %s AND revoked_at IS NULL", (waiver_id,)
    ).fetchone()
    return row[0] if row else None


def revoke_waiver(conn, waiver_id: str, now: datetime) -> bool:
    """Marks a waiver revoked. True if it was active."""
    # The API turns False (unknown or already revoked) into a 404.
    return (
        conn.execute(
            "UPDATE waivers SET revoked_at = %s WHERE waiver_id = %s AND revoked_at IS NULL",
            (now, waiver_id),
        ).rowcount
        == 1
    )


def recent_final(conn, service: str, limit: int) -> list[dict]:
    """The latest final decisions for a service, used to backtest a team policy change."""
    # Only final decisions: pending ones never ran the rules, so there's nothing to compare.
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """SELECT decision_id, stage, outcome, input FROM decisions
               WHERE service = %s AND status = 'final' ORDER BY created_at DESC LIMIT %s""",
            (service, limit),
        ).fetchall()


def insert_kill_switch(conn, k: dict) -> None:
    """Stores a new kill switch."""
    conn.execute(
        """INSERT INTO kill_switches (kill_id, rule, reason, created_by, created_at, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (k["kill_id"], k["rule"], k["reason"], k["created_by"], k["created_at"], k["expires_at"]),
    )


def active_kill_switches(conn, now: datetime) -> list[dict]:
    """Kill switches that haven't expired or been cleared."""
    # No service filter: a kill switch applies to every service.
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """SELECT kill_id, rule, reason, created_by, expires_at FROM kill_switches
               WHERE expires_at > %s AND cleared_at IS NULL ORDER BY created_at""",
            (now,),
        ).fetchall()
    return [{**r, "expires_at": r["expires_at"].isoformat()} for r in rows]


def clear_kill_switch(conn, kill_id: str, now: datetime) -> bool:
    """Switches a rule back on. True if it was off."""
    return (
        conn.execute(
            "UPDATE kill_switches SET cleared_at = %s WHERE kill_id = %s AND cleared_at IS NULL",
            (now, kill_id),
        ).rowcount
        == 1
    )


BREAK_GLASS_VALID = timedelta(hours=24)


def insert_break_glass(conn, b: dict) -> None:
    """Records a break-glass, so the next decision for that change lets it through."""
    # Keyed by service, change (subject) and stage, which is how the next decision finds it.
    conn.execute(
        """INSERT INTO break_glass (break_glass_id, decision_id, service, subject, stage, reason,
                                    ticket, created_by, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            b["break_glass_id"],
            b["decision_id"],
            b["service"],
            b["subject"],
            b["stage"],
            b["reason"],
            b["ticket"],
            b["created_by"],
            b["created_at"],
        ),
    )


def break_glass(conn, service: str, subject: str, stage: str, now: datetime) -> dict | None:
    """The latest break-glass for this change and stage, if it is still valid."""
    row = conn.execute(
        """SELECT break_glass_id, reason, ticket, created_by FROM break_glass
           WHERE service = %s AND subject = %s AND stage = %s AND created_at > %s
           ORDER BY created_at DESC LIMIT 1""",
        (service, subject, stage, now - BREAK_GLASS_VALID),
    ).fetchone()
    # Shaped for the policy input: the Rego reads by, reason and ticket.
    return {"id": row[0], "reason": row[1], "ticket": row[2], "by": row[3]} if row else None


def expiring_waivers(conn, now: datetime, within: timedelta) -> list[dict]:
    """Active waivers that expire soon and haven't been reminded about yet."""
    # reminded_at IS NULL makes it one reminder per waiver.
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """SELECT waiver_id, service, rule, ticket, approver, expires_at FROM waivers
               WHERE revoked_at IS NULL AND reminded_at IS NULL
                 AND expires_at > %s AND expires_at <= %s""",
            (now, now + within),
        ).fetchall()


def reminded(conn, waiver_id: str, now: datetime) -> None:
    """Marks a waiver as reminded, so the next sweep doesn't post again."""
    conn.execute("UPDATE waivers SET reminded_at = %s WHERE waiver_id = %s", (now, waiver_id))


def portal_view(conn, now: datetime) -> dict:
    """Everything the portal page shows, newest first."""
    with conn.cursor(row_factory=dict_row) as cur:
        q = lambda sql, *a: cur.execute(sql, a).fetchall()  # noqa: E731
        return {
            "waivers": q(
                """SELECT waiver_id, service, rule, items, ticket, reason, approver, expires_at
                   FROM waivers
                            WHERE revoked_at IS NULL AND expires_at > %s ORDER BY expires_at""",
                now,
            ),
            "kill_switches": q(
                """SELECT kill_id, rule, reason, created_by, expires_at FROM kill_switches
                                  WHERE cleared_at IS NULL AND expires_at > %s""",
                now,
            ),
            "break_glass": q(
                """SELECT service, stage, subject, reason, ticket, created_by, created_at
                                FROM break_glass WHERE created_at > %s ORDER BY created_at DESC""",
                now - timedelta(days=7),
            ),
            "team_policies": q("""SELECT DISTINCT ON (service)
                                         service, version, published_by, published_at
                                  FROM team_policies ORDER BY service, published_at DESC"""),
            "decisions": q("""SELECT decision_id, service, stage, status, outcome, created_at
                              FROM decisions
                              ORDER BY created_at DESC LIMIT 20"""),
        }
