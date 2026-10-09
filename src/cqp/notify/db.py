"""Gate jobs waiting on evidence, so they can be retried once it arrives or its deadline passes."""

import psycopg
from psycopg.types.json import Jsonb

SCHEMA = """
-- One row per waiting gate job. If the job asks again, its row is updated, not duplicated.
CREATE TABLE IF NOT EXISTS waiting_pipelines (
    project  text NOT NULL,
    job_id   text NOT NULL,
    service  text NOT NULL,
    subject  text NOT NULL,
    stage    text NOT NULL,
    checks   jsonb NOT NULL,
    deadline timestamptz NOT NULL,
    PRIMARY KEY (project, job_id)
);
"""


def migrate(conn: psycopg.Connection) -> None:
    """Creates the table if it doesn't exist yet."""
    conn.execute(SCHEMA)
    conn.commit()


def remember(conn, project, job_id, service, subject, stage, checks, deadline) -> None:
    """Records a gate job that is waiting on evidence."""
    # job_id is stored as text, so it compares the same
    # whether it came from a token or the database.
    conn.execute(
        """INSERT INTO waiting_pipelines
               (project, job_id, service, subject, stage, checks, deadline)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (project, job_id) DO UPDATE SET checks = EXCLUDED.checks,
                                                       deadline = EXCLUDED.deadline""",
        (project, str(job_id), service, subject, stage, Jsonb(checks), deadline),
    )


def forget(conn, service, subject, stage) -> None:
    # A final decision for this change and stage ends the wait for every job on it.
    conn.execute(
        "DELETE FROM waiting_pipelines WHERE service = %s AND subject = %s AND stage = %s",
        (service, subject, stage),
    )


def waiting(conn) -> list[tuple]:
    """Every gate job still waiting."""
    return conn.execute(
        "SELECT project, job_id, service, subject, stage, checks, deadline "
        "FROM waiting_pipelines"
    ).fetchall()


def done(conn, project, job_id) -> None:
    """Removes a gate job once it has been retried."""
    conn.execute(
        "DELETE FROM waiting_pipelines WHERE project = %s AND job_id = %s", (project, job_id)
    )
