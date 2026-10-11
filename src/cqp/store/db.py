"""Postgres evidence store: signals plus the commit -> image link."""

from datetime import datetime

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    event_id    text PRIMARY KEY,
    service     text NOT NULL,
    commit      text NOT NULL,
    image       text,
    check_name  text NOT NULL,
    kind        text NOT NULL,
    status      text NOT NULL,
    run         text NOT NULL,
    producer    text NOT NULL,
    payload     jsonb NOT NULL,
    raw_ref     text,
    observed_at timestamptz NOT NULL,
    stored_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS signals_by_change
    ON signals (service, commit, check_name, observed_at DESC);
-- Whether the sender is trusted for this result: a CI upload from a protected branch, or a
-- signed webhook. Rows written before this column existed keep the old behaviour (trusted).
ALTER TABLE signals ADD COLUMN IF NOT EXISTS trusted boolean NOT NULL DEFAULT true;

-- Which commit each image was built from, so results on the commit (unit tests, say)
-- also count for the image when it is deployed later.
CREATE TABLE IF NOT EXISTS builds (
    service text NOT NULL,
    image   text NOT NULL,
    commit  text NOT NULL,
    PRIMARY KEY (service, image)
);
"""


def migrate(conn: psycopg.Connection) -> None:
    """Creates the tables if they don't exist yet."""
    conn.execute(SCHEMA)
    conn.commit()


def save(conn: psycopg.Connection, s: dict) -> None:
    """Stores one signal, and the image-to-commit link if it's a provenance record."""
    # Same event_id again (a retried upload or a re-read message) is a no-op.
    conn.execute(
        """INSERT INTO signals (event_id, service, commit, image, check_name, kind, status, run,
                                producer, payload, raw_ref, observed_at, trusted)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (event_id) DO NOTHING""",
        (
            s["event_id"],
            s["service"],
            s["commit"],
            s["image"],
            s["check"],
            s["kind"],
            s["status"],
            s["run"],
            s["producer"],
            Jsonb(s["payload"]),
            s["raw_ref"],
            s["observed_at"],
            s.get("trusted", True),
        ),
    )
    # Only a trusted build links an image to a commit. Otherwise any branch job could claim
    # "image X was built from commit Y" first, and X would inherit Y's evidence in production.
    if s["check"] == "build.provenance" and s.get("trusted", True):
        conn.execute(
            "INSERT INTO builds (service, image, commit) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (s["service"], s["image"], s["commit"]),
        )


# The rows about one change: about the image (scans), about the commit (tests), or about the
# commit the image was built from. Shared by every query that looks at "this change".
SCOPE = sql.SQL("""(s.image = %(image)s OR s.commit = %(commit)s
            OR s.commit IN (SELECT commit FROM builds
                            WHERE service = %(service)s AND image = %(image)s))""")


def latest(
    conn: psycopg.Connection,
    service: str,
    image: str | None,
    commit: str | None = None,
    trusted_only: bool = False,
) -> dict[str, dict]:
    """Newest result per check for an image (including results on its commit) or a bare commit.

    trusted_only (prod-deploy): a result about the image from an unprotected branch, on some
    other commit, may only make things stricter. Its failure counts, its pass doesn't.
    """
    # The newest run per check wins, so a passing re-run replaces an earlier failure.
    # One run may send several reports for a check (two test modules): any failure among them wins.
    rows = conn.execute(
        sql.SQL(
            """WITH matching AS (
             SELECT check_name, kind, status, payload, event_id, run, producer, observed_at,
                    stored_at,
                    first_value(run) OVER newest AS newest_run,
                    first_value(producer) OVER newest AS newest_producer
             FROM signals s
             WHERE s.service = %(service)s AND {scope}
               AND (NOT %(trusted_only)s OR s.trusted OR s.status = 'fail'
                    OR s.commit IN (SELECT commit FROM builds
                                    WHERE service = %(service)s AND image = %(image)s))
             -- observed_at is when the result was produced, not when it arrived, so a late
             -- upload from an older run doesn't win. stored_at only breaks exact ties.
             WINDOW newest AS (PARTITION BY check_name ORDER BY observed_at DESC, stored_at DESC)
           )
           SELECT DISTINCT ON (check_name) check_name, kind, status, payload, event_id, observed_at
           FROM matching WHERE run = newest_run AND producer = newest_producer
           ORDER BY check_name, (status = 'fail') DESC, observed_at DESC, stored_at DESC"""
        ).format(scope=SCOPE),
        {"service": service, "image": image, "commit": commit, "trusted_only": trusted_only},
    ).fetchall()
    return {
        r[0]: {
            "kind": r[1],
            "status": r[2],
            "payload": r[3],
            "event_id": r[4],
            "observed_at": r[5].isoformat(),
        }
        for r in rows
    }


def reruns(
    conn: psycopg.Connection,
    service: str,
    image: str | None,
    commit: str | None,
    evidence: dict[str, dict],
) -> dict[str, dict]:
    """Checks that pass now but failed in an earlier run of the same change, and what failed.

    Same change, so the code didn't change between the runs: that is what flaky looks like.
    """
    passed = {
        check: datetime.fromisoformat(e["observed_at"])
        for check, e in evidence.items()
        if e["status"] == "pass" and e["kind"] == "outcome"
    }
    if not passed:
        return {}
    rows = conn.execute(
        sql.SQL(
            """SELECT check_name, run, observed_at, payload->'failing' FROM signals s
            WHERE s.service = %(service)s AND s.status = 'fail'
              AND s.check_name = ANY(%(checks)s) AND {scope}"""
        ).format(scope=SCOPE),
        {"service": service, "image": image, "commit": commit, "checks": list(passed)},
    ).fetchall()
    found: dict[str, tuple[set, set]] = {}
    for check, run, observed_at, failing in rows:
        if observed_at < passed[check]:
            runs, items = found.setdefault(check, (set(), set()))
            runs.add(run)
            items.update(failing if isinstance(failing, list) else [])
    return {c: {"failed_runs": len(r), "items": sorted(i)} for c, (r, i) in sorted(found.items())}


def baseline(
    conn: psycopg.Connection,
    service: str,
    checks: list[str],
    image: str | None,
    commit: str | None,
) -> dict[str, dict]:
    """Each check's last trusted result on another change: what the protected branch looks like.

    It tells a finding this change introduced from one that was already there, and gives a
    measurement something to compare with (p95 up 20%).
    """
    if not checks:
        return {}
    # coalesce: with no image (or no commit) SCOPE is NULL rather than false.
    rows = conn.execute(
        sql.SQL(
            """SELECT check_name, run, producer, commit, status, payload FROM signals s
            WHERE s.service = %(service)s AND s.trusted AND s.check_name = ANY(%(checks)s)
              AND NOT coalesce({scope}, false)
            ORDER BY check_name, observed_at DESC, stored_at DESC"""
        ).format(scope=SCOPE),
        {"service": service, "image": image, "commit": commit, "checks": checks},
    ).fetchall()
    result: dict[str, dict] = {}
    newest: dict[str, tuple] = {}
    for check, run, producer, base_commit, _status, payload in rows:
        # Only the newest run of each check; its reports (several modules) are merged.
        if newest.setdefault(check, (run, producer)) != (run, producer):
            continue
        b = result.setdefault(check, {"commit": base_commit, "failing": [], "value": None})
        b["failing"] = sorted(set(b["failing"]) | set(payload.get("failing") or []))
        if b["value"] is None and isinstance(payload.get("value"), int | float):
            b["value"] = payload["value"]
    return result


def flaky(conn: psycopg.Connection, service: str, since: datetime) -> list[dict]:
    """Tests that failed and then passed on the same commit: candidates for quarantine."""
    rows = conn.execute(
        """SELECT f.check_name, t.test, count(DISTINCT f.commit), max(f.observed_at)
           FROM signals f
           CROSS JOIN LATERAL jsonb_array_elements_text(
               coalesce(f.payload->'failing', '[]'::jsonb)) AS t(test)
           WHERE f.service = %s AND f.status = 'fail' AND f.kind = 'outcome'
             AND f.observed_at > %s
             AND EXISTS (SELECT 1 FROM signals p
                         WHERE p.service = f.service AND p.commit = f.commit
                           AND p.check_name = f.check_name AND p.status = 'pass'
                           AND p.observed_at > f.observed_at
                           -- the whole later run passed, not just one of its modules
                           AND NOT EXISTS (SELECT 1 FROM signals q
                                           WHERE q.run = p.run AND q.producer = p.producer
                                             AND q.check_name = p.check_name
                                             AND q.status = 'fail'))
           GROUP BY f.check_name, t.test
           ORDER BY count(DISTINCT f.commit) DESC, max(f.observed_at) DESC
           LIMIT 50""",
        (service, since),
    ).fetchall()
    return [
        {"check": c, "test": t, "commits": n, "last_seen": seen.isoformat()}
        for c, t, n, seen in rows
    ]
