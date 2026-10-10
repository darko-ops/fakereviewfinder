"""Reading results back: connections, evidence, summaries, exports, refresh diffs.

Review text is untrusted. The CLI prints it as data; CSV cells that a
spreadsheet would evaluate as a formula are neutralised on export.
"""

from __future__ import annotations

import csv
from collections import Counter
import io
import json
import sqlite3
from dataclasses import dataclass

from .collect import SAMPLING_LABEL
from .db import transaction
from .models import utcnow


@dataclass
class Filters:
    min_shared: int = 3
    max_distance_km: float | None = None
    min_rating: int | None = None
    max_rating: int | None = None
    since: str | None = None  # YYYY-MM-DD, destination review date
    until: str | None = None
    category: str | None = None

    @property
    def filters_evidence(self) -> bool:
        return any(v is not None for v in (self.min_rating, self.max_rating, self.since, self.until))


def run_summary(conn: sqlite3.Connection, run_id: int) -> dict:
    run = conn.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise KeyError(f"no run {run_id}")
    seed = conn.execute(
        "SELECT b.* FROM investigation i JOIN business b ON b.id=i.seed_business_id WHERE i.id=?",
        (run["investigation_id"],),
    ).fetchone()
    analysis = json.loads(run["analysis"]) if run["analysis"] else None
    return {
        "run_id": run["id"],
        "investigation_id": run["investigation_id"],
        "status": run["status"],
        "stop_reason": run["stop_reason"],
        "provider": run["provider"],
        "sampling_policy": run["sampling_policy"],
        "sampling_label": SAMPLING_LABEL.get(run["sampling_policy"]),
        "limits": json.loads(run["limits"]),
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "request_count": run["request_count"],
        "cost": run["cost"],
        "cost_estimate": run["cost_estimate"],
        "seed": _business_dict(conn, seed),
        "analysis": analysis,
    }


def connections(conn: sqlite3.Connection, run_id: int, f: Filters | None = None) -> list[dict]:
    """Destinations sorted by shared reviewers, then name, then ID.

    Rating and date filters narrow the evidence; the row then reports the
    filtered count beside the unfiltered one rather than replacing it.
    """
    f = f or Filters()
    out = []
    for c in conn.execute("SELECT * FROM connection WHERE run_id=?", (run_id,)).fetchall():
        signals = json.loads(c["signals"])
        dest = conn.execute("SELECT * FROM business WHERE id=?", (c["destination_id"],)).fetchone()
        if f.max_distance_km is not None and (
            signals["distance_km"] is None or signals["distance_km"] > f.max_distance_km
        ):
            continue
        if f.category and (dest["category"] or "").casefold() != f.category.casefold():
            continue
        matching = c["shared_count"]
        if f.filters_evidence:
            matching = sum(1 for ev in evidence(conn, c["id"]) if _evidence_matches(ev, f))
        if matching < f.min_shared:
            continue
        out.append({
            "connection_id": c["id"],
            "destination": _business_dict(conn, dest),
            "shared_reviewers": c["shared_count"],
            "shared_reviewers_matching_filters": matching if f.filters_evidence else None,
            "retrieved_denominator": c["retrieved_denominator"],
            "selected_denominator": c["selected_denominator"],
            "share_of_retrieved": _ratio(c["shared_count"], c["retrieved_denominator"]),
            "share_of_selected": _ratio(c["shared_count"], c["selected_denominator"]),
            "signals": signals,
            "analysis_version": c["analysis_version"],
        })
    out.sort(key=lambda r: (-r["shared_reviewers"], (r["destination"]["name"] or "").casefold(), r["destination"]["id"]))
    return out


def evidence(conn: sqlite3.Connection, connection_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT e.*, rv.external_id AS reviewer_external_id, rv.display_name, rv.profile_url,
               obs_s.rating AS seed_rating, obs_s.text AS seed_text, obs_s.published_raw AS seed_published_raw,
               obs_s.published_at AS seed_published_at, obs_s.timestamp_precision AS seed_precision,
               obs_s.source_url AS seed_source_url, obs_s.fetched_at AS seed_fetched_at,
               sr.external_review_id AS seed_external_review_id,
               obs_d.rating AS dest_rating, obs_d.text AS dest_text, obs_d.published_raw AS dest_published_raw,
               obs_d.published_at AS dest_published_at, obs_d.timestamp_precision AS dest_precision,
               obs_d.source_url AS dest_source_url, obs_d.fetched_at AS dest_fetched_at,
               dr.external_review_id AS dest_external_review_id
        FROM connection_evidence e
        JOIN reviewer rv ON rv.id = e.reviewer_id
        JOIN review_observation obs_s ON obs_s.id = e.seed_observation_id
        JOIN review sr ON sr.id = e.seed_review_id
        JOIN review_observation obs_d ON obs_d.id = e.destination_observation_id
        JOIN review dr ON dr.id = e.destination_review_id
        WHERE e.connection_id = ?
        ORDER BY rv.external_id
        """,
        (connection_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _evidence_matches(ev: dict, f: Filters) -> bool:
    if f.min_rating is not None and (ev["dest_rating"] is None or ev["dest_rating"] < f.min_rating):
        return False
    if f.max_rating is not None and (ev["dest_rating"] is None or ev["dest_rating"] > f.max_rating):
        return False
    if f.since or f.until:
        # Only day-or-better dates can be placed in a range.
        if ev["dest_precision"] not in ("exact", "day") or not ev["dest_published_at"]:
            return False
        d = ev["dest_published_at"][:10]
        if f.since and d < f.since:
            return False
        if f.until and d > f.until:
            return False
    return True


# -- exports ----------------------------------------------------------------

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def csv_safe(value) -> object:
    """Neutralise a cell a spreadsheet would treat as a formula."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


EVIDENCE_COLUMNS = [
    "run_id", "analysis_version", "seed_business_id", "seed_name", "destination_business_id", "destination_name",
    "destination_address", "destination_category", "destination_source_url",
    "shared_reviewers", "retrieved_denominator", "selected_denominator", "share_of_retrieved", "share_of_selected",
    "retrieval_coverage", "run_status",
    "reviewer_external_id", "reviewer_display_name", "reviewer_profile_url", "reviewer_history_status",
    "seed_review_id", "seed_rating", "seed_published_raw", "seed_published_at", "seed_timestamp_precision",
    "seed_source_url", "seed_fetched_at", "seed_text",
    "destination_review_id", "destination_rating", "destination_published_raw", "destination_published_at",
    "destination_timestamp_precision", "destination_review_source_url", "destination_fetched_at", "destination_text",
    "within_window", "similar_text",
]


def evidence_csv(conn: sqlite3.Connection, run_id: int, f: Filters | None = None) -> str:
    """One row per shared account per connection, every count with its
    denominator and the run's coverage beside it."""
    f = f or Filters(min_shared=1)
    summary = run_summary(conn, run_id)
    seed = summary["seed"]
    statuses = {
        r["reviewer_id"]: r["status"]
        for r in conn.execute("SELECT reviewer_id, status FROM profile_fetch WHERE run_id=?", (run_id,))
    }
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(EVIDENCE_COLUMNS)
    coverage = (summary["analysis"] or {}).get("retrieval_coverage")
    for c in connections(conn, run_id, f):
        d = c["destination"]
        for ev in evidence(conn, c["connection_id"]):
            row = [
                run_id, c["analysis_version"], seed["external_id"], seed["name"], d["external_id"], d["name"],
                d["address"], d["category"], d["source_url"],
                c["shared_reviewers"], c["retrieved_denominator"], c["selected_denominator"],
                c["share_of_retrieved"], c["share_of_selected"], coverage, summary["status"],
                ev["reviewer_external_id"], ev["display_name"], ev["profile_url"], statuses.get(ev["reviewer_id"]),
                ev["seed_external_review_id"], ev["seed_rating"], ev["seed_published_raw"], ev["seed_published_at"],
                ev["seed_precision"], ev["seed_source_url"], ev["seed_fetched_at"], ev["seed_text"],
                ev["dest_external_review_id"], ev["dest_rating"], ev["dest_published_raw"], ev["dest_published_at"],
                ev["dest_precision"], ev["dest_source_url"], ev["dest_fetched_at"], ev["dest_text"],
                ev["within_window"], ev["similar_text"],
            ]
            w.writerow([csv_safe(v) for v in row])
    return buf.getvalue()


def run_json(conn: sqlite3.Connection, run_id: int) -> dict:
    """Everything needed to reproduce the run's counts: settings, coverage by
    status, every connection with denominators and its evidence."""
    summary = run_summary(conn, run_id)
    profiles = [
        {
            "reviewer_external_id": r["external_id"],
            "display_name": r["display_name"],
            "profile_url": r["profile_url"],
            "status": r["status"],
            "fetched_count": r["fetched_count"],
            "reported_total": r["reported_total"],
            "capped": bool(r["capped"]),
            "error_code": r["error_code"],
            "fetched_at": r["fetched_at"],
            "from_cache_run_id": r["source_run_id"],
        }
        for r in conn.execute(
            "SELECT p.*, rv.external_id, rv.display_name, rv.profile_url FROM run_seed_reviewer s"
            " JOIN profile_fetch p ON p.run_id=s.run_id AND p.reviewer_id=s.reviewer_id"
            " JOIN reviewer rv ON rv.id=s.reviewer_id WHERE s.run_id=? ORDER BY s.position",
            (run_id,),
        )
    ]
    conns = []
    for c in connections(conn, run_id, Filters(min_shared=1)):
        c = dict(c)
        c["evidence"] = [_evidence_export(ev) for ev in evidence(conn, c.pop("connection_id"))]
        conns.append(c)
    return {"exported_at": utcnow(), **summary, "profiles": profiles, "connections": conns}


def _evidence_export(ev: dict) -> dict:
    return {
        "reviewer": {
            "external_id": ev["reviewer_external_id"],
            "display_name": ev["display_name"],
            "profile_url": ev["profile_url"],
        },
        "seed_review": _review_export(ev, "seed"),
        "destination_review": _review_export(ev, "dest"),
        "within_window": ev["within_window"],
        "similar_text": bool(ev["similar_text"]),
    }


def _review_export(ev: dict, prefix: str) -> dict:
    return {
        "external_review_id": ev[f"{prefix}_external_review_id"],
        "rating": ev[f"{prefix}_rating"],
        "text": ev[f"{prefix}_text"],
        "published_raw": ev[f"{prefix}_published_raw"],
        "published_at": ev[f"{prefix}_published_at"],
        "timestamp_precision": ev[f"{prefix}_precision"],
        "source_url": ev[f"{prefix}_source_url"],
        "fetched_at": ev[f"{prefix}_fetched_at"],
    }


# -- refresh snapshots -----------------------------------------------------

NOTIFY_MIN_ACCOUNTS = 3


def compare_runs(conn: sqlite3.Connection, earlier: int, later: int) -> dict:
    """What a later run observed that an earlier one did not, and the reverse.

    A review seen earlier and not seen later is "not observed in the later
    fetch", together with how that later fetch ended. It is never reported as
    deleted: a capped, failed or otherwise partial fetch does not show absence,
    and even a complete one shows only what the provider returned that day.
    """
    def items(run_id):
        rows = conn.execute(
            "SELECT f.reviewer_id, f.status, i.review_id FROM profile_fetch f LEFT JOIN profile_fetch_item i"
            " ON i.reviewer_id=f.reviewer_id AND i.run_id=COALESCE(f.source_run_id, f.run_id) WHERE f.run_id=?",
            (run_id,),
        ).fetchall()
        status = {r["reviewer_id"]: r["status"] for r in rows}
        seen = {(r["reviewer_id"], r["review_id"]) for r in rows if r["review_id"] is not None}
        return status, seen

    def conn_accounts(run_id):
        out = {}
        for c in conn.execute("SELECT id, destination_id FROM connection WHERE run_id=?", (run_id,)):
            out[c["destination_id"]] = {
                r[0] for r in conn.execute("SELECT reviewer_id FROM connection_evidence WHERE connection_id=?", (c["id"],))
            }
        return out

    status_a, seen_a = items(earlier)
    status_b, seen_b = items(later)
    not_observed_later = Counter(status_b.get(rid, "not_selected") for rid, _ in seen_a - seen_b)
    ca, cb = conn_accounts(earlier), conn_accounts(later)
    changes = []
    for dest, accounts in cb.items():
        before = ca.get(dest, set())
        added = accounts - before
        if not added:
            continue
        kind = "new_connection" if not before else "connection_gained_accounts"
        changes.append({"destination_id": dest, "kind": kind, "newly_observed_accounts": len(added),
                        "shared_before": len(before), "shared_after": len(accounts)})
    sa = json.loads(conn.execute("SELECT analysis FROM run WHERE id=?", (earlier,)).fetchone()[0] or "{}")
    sb = json.loads(conn.execute("SELECT analysis FROM run WHERE id=?", (later,)).fetchone()[0] or "{}")
    return {
        "earlier_run_id": earlier,
        "later_run_id": later,
        "new_observations": len(seen_b - seen_a),
        "not_observed_in_later_fetch": sum(not_observed_later.values()),
        "not_observed_by_later_fetch_status": dict(sorted(not_observed_later.items())),
        "note": "Reviews not observed in a later fetch are not reported as deleted.",
        "coverage_before": sa.get("coverage"),
        "coverage_after": sb.get("coverage"),
        "retrieval_coverage_before": sa.get("retrieval_coverage"),
        "retrieval_coverage_after": sb.get("retrieval_coverage"),
        "connection_changes": sorted(changes, key=lambda c: (-c["newly_observed_accounts"], c["destination_id"])),
    }


def record_notifications(conn: sqlite3.Connection, earlier: int, later: int) -> list[str]:
    """In-app notices for observation changes worth a look: a new connection
    from at least three newly observed accounts, or an existing one gaining at
    least three. Worded as observation changes, not new review activity."""
    diff = compare_runs(conn, earlier, later)
    inv = conn.execute("SELECT investigation_id FROM run WHERE id=?", (later,)).fetchone()[0]
    messages = []
    for ch in diff["connection_changes"]:
        if ch["newly_observed_accounts"] < NOTIFY_MIN_ACCOUNTS:
            continue
        name = conn.execute("SELECT name FROM business WHERE id=?", (ch["destination_id"],)).fetchone()[0]
        if ch["kind"] == "new_connection":
            msg = f"New observed connection: {name} ({ch['shared_after']} shared reviewers newly observed)."
        else:
            msg = (f"Observed overlap with {name} grew from {ch['shared_before']} to {ch['shared_after']} "
                   f"shared reviewers. This is a change in what was observed, not proof of new reviews.")
        messages.append(msg)
    with transaction(conn):
        conn.executemany(
            "INSERT INTO notification (investigation_id, run_id, created_at, kind, message) VALUES (?,?,?,?,?)",
            [(inv, later, utcnow(), "observation_change", m) for m in messages],
        )
    return messages


def _business_dict(conn: sqlite3.Connection, b: sqlite3.Row) -> dict:
    ext = conn.execute(
        "SELECT id_type, external_id FROM business_external_id WHERE business_id=? AND verified_by IS NULL"
        " ORDER BY rowid LIMIT 1",
        (b["id"],),
    ).fetchone()
    return {
        "id": b["id"],
        "external_id": ext["external_id"] if ext else None,
        "id_type": ext["id_type"] if ext else None,
        "name": b["name"],
        "address": b["address"],
        "latitude": b["latitude"],
        "longitude": b["longitude"],
        "category": b["category"],
        "source_url": b["source_url"],
    }


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None
