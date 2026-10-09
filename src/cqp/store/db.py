"""Postgres evidence store: signals plus the commit -> image link."""

import psycopg
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
                                producer, payload, raw_ref, observed_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        ),
    )
    if s["check"] == "build.provenance":
        conn.execute(
            "INSERT INTO builds (service, image, commit) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (s["service"], s["image"], s["commit"]),
        )


def latest(
    conn: psycopg.Connection, service: str, image: str | None, commit: str | None = None
) -> dict[str, dict]:
    """Newest result per check for an image (including results on its commit) or a bare commit."""
    # A result can be about the image (scans), the commit (tests), or the commit the image was
    # built from. Newest per check wins, so a passing re-run replaces an earlier failure.
    rows = conn.execute(
        """SELECT DISTINCT ON (check_name) check_name, kind, status, payload, event_id
           FROM signals s
           WHERE s.service = %s
             AND (s.image = %s OR s.commit = %s
                  OR s.commit IN (SELECT commit FROM builds WHERE service = %s AND image = %s))
           ORDER BY check_name, observed_at DESC""",
        (service, image, commit, service, image),
    ).fetchall()
    return {r[0]: {"kind": r[1], "status": r[2], "payload": r[3], "event_id": r[4]} for r in rows}
