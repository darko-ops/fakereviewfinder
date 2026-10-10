"""Retention, deletion and the refresh schedule.

Defaults follow spec §11 (90 days of raw payloads, one year of normalised
observations) and are subject to the provider's terms once one is chosen.
Weekly refresh is off until enabled per investigation; this module only
reports which investigations are due, it schedules nothing by itself.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .collect import ACTIVE_RUN_STATUSES
from .db import transaction

RAW_PAYLOAD_DAYS = 90
OBSERVATION_DAYS = 365
REFRESH_INTERVAL = {"weekly": timedelta(days=7)}


def _cutoff(now: datetime, days: float) -> str:
    return (now - timedelta(days=days)).replace(microsecond=0).isoformat()


def purge(
    conn: sqlite3.Connection,
    raw_days: float = RAW_PAYLOAD_DAYS,
    observation_days: float = OBSERVATION_DAYS,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    with transaction(conn):
        raw = conn.execute("DELETE FROM raw_payload WHERE fetched_at < ?", (_cutoff(now, raw_days),)).rowcount
        # Observations belong to runs, so old runs go whole, except one a newer
        # run still reads cached histories from.
        old_runs = [
            r[0] for r in conn.execute(
                "SELECT id FROM run WHERE COALESCE(finished_at, started_at) < ? AND id NOT IN"
                " (SELECT source_run_id FROM profile_fetch WHERE source_run_id IS NOT NULL"
                "  AND run_id NOT IN (SELECT id FROM run WHERE COALESCE(finished_at, started_at) < ?))",
                (_cutoff(now, observation_days), _cutoff(now, observation_days)),
            )
        ]
        for rid in old_runs:
            _delete_run(conn, rid)
        orphans = _delete_orphans(conn)
    return {"raw_payloads_deleted": raw, "runs_deleted": len(old_runs), **orphans}


def delete_investigation(conn: sqlite3.Connection, investigation_id: int) -> dict:
    """Delete an investigation, its runs, and any identity no other
    investigation still references."""
    with transaction(conn):
        runs = [r[0] for r in conn.execute("SELECT id FROM run WHERE investigation_id=?", (investigation_id,))]
        # Another investigation's run that borrowed a cached history from one
        # of these loses it: the profile becomes unknown with no records, and
        # that run is re-analysed below so its counts stop relying on it.
        affected = {
            r[0] for rid in runs for r in conn.execute(
                "SELECT run_id FROM profile_fetch WHERE source_run_id=? AND run_id NOT IN"
                " (SELECT id FROM run WHERE investigation_id=?)",
                (rid, investigation_id),
            )
        }
        for rid in affected:
            conn.execute(
                "DELETE FROM connection_evidence WHERE connection_id IN (SELECT id FROM connection WHERE run_id=?)",
                (rid,),
            )
            conn.execute("DELETE FROM connection WHERE run_id=?", (rid,))
        for rid in runs:
            conn.execute(
                "UPDATE profile_fetch SET source_run_id=NULL, status='unknown', fetched_count=0,"
                " error_code='cache_source_deleted' WHERE source_run_id=?",
                (rid,),
            )
        for rid in runs:
            _delete_run(conn, rid)
        conn.execute("DELETE FROM investigation WHERE id=?", (investigation_id,))
        orphans = _delete_orphans(conn)
    from .analyze import analyze_run

    for rid in sorted(affected):
        analyze_run(conn, rid)
    return {"runs_deleted": len(runs), "runs_reanalysed": len(affected), **orphans}


def _delete_run(conn: sqlite3.Connection, run_id: int) -> None:
    for table in ("notification", "raw_payload"):
        conn.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
    conn.execute(
        "DELETE FROM connection_evidence WHERE connection_id IN (SELECT id FROM connection WHERE run_id=?)", (run_id,)
    )
    for table in ("connection", "profile_fetch_item", "profile_fetch", "run_seed_reviewer", "review_observation"):
        conn.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM run WHERE id=?", (run_id,))


def _delete_orphans(conn: sqlite3.Connection) -> dict:
    reviews = conn.execute(
        "DELETE FROM review WHERE id NOT IN (SELECT review_id FROM review_observation)"
    ).rowcount
    reviewers = conn.execute(
        "DELETE FROM reviewer WHERE id NOT IN (SELECT reviewer_id FROM review)"
        " AND id NOT IN (SELECT reviewer_id FROM profile_fetch)"
    ).rowcount
    businesses = conn.execute(
        "DELETE FROM business WHERE id NOT IN (SELECT business_id FROM review)"
        " AND id NOT IN (SELECT seed_business_id FROM investigation)"
    ).rowcount
    conn.execute("DELETE FROM business_external_id WHERE business_id NOT IN (SELECT id FROM business)")
    return {"reviews_deleted": reviews, "reviewers_deleted": reviewers, "businesses_deleted": businesses}


def set_refresh_schedule(conn: sqlite3.Connection, investigation_id: int, schedule: str | None) -> None:
    if schedule is not None and schedule not in REFRESH_INTERVAL:
        raise ValueError(f"unknown schedule {schedule}")
    with transaction(conn):
        conn.execute("UPDATE investigation SET refresh_schedule=? WHERE id=?", (schedule, investigation_id))


def due_refreshes(conn: sqlite3.Connection, now: datetime | None = None) -> list[int]:
    """Investigations with a schedule, no active run, and a last run older
    than the interval. A scheduler enqueues these; it never starts a second
    active refresh for the same investigation."""
    now = now or datetime.now(timezone.utc)
    due = []
    for inv in conn.execute("SELECT id, refresh_schedule FROM investigation WHERE refresh_schedule IS NOT NULL"):
        placeholders = ",".join("?" * len(ACTIVE_RUN_STATUSES))
        active = conn.execute(
            f"SELECT 1 FROM run WHERE investigation_id=? AND status IN ({placeholders})",
            (inv["id"], *ACTIVE_RUN_STATUSES),
        ).fetchone()
        if active:
            continue
        last = conn.execute(
            "SELECT MAX(COALESCE(finished_at, started_at)) FROM run WHERE investigation_id=?", (inv["id"],)
        ).fetchone()[0]
        if last is None or datetime.fromisoformat(last) <= now - REFRESH_INTERVAL[inv["refresh_schedule"]]:
            due.append(inv["id"])
    return due
