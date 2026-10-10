"""Normalisation: provider records into canonical identities and observations."""

from __future__ import annotations

import hashlib
import sqlite3

from .models import PLATFORM, BusinessRecord, ReviewRecord, ReviewerRef, parse_timestamp


def business_id_for(conn: sqlite3.Connection, b: BusinessRecord) -> int | None:
    row = conn.execute(
        "SELECT business_id FROM business_external_id WHERE platform=? AND id_type=? AND external_id=?",
        (PLATFORM, b.id_type, b.external_id),
    ).fetchone()
    return row[0] if row else None


def upsert_business(conn: sqlite3.Connection, b: BusinessRecord, observed_at: str, provider: str) -> int | None:
    """Return the internal ID, creating the business on first sight.

    Matching is by (platform, id_type, external_id) or a provider-verified
    alias, never by name or address. Descriptive fields fill in when known.
    """
    if not b.external_id:
        return None
    bid = business_id_for(conn, b)
    if bid is None:
        for id_type, ext in b.verified_aliases:
            row = conn.execute(
                "SELECT business_id FROM business_external_id WHERE platform=? AND id_type=? AND external_id=?",
                (PLATFORM, id_type, ext),
            ).fetchone()
            if row:
                bid = row[0]
                break
    if bid is None:
        bid = conn.execute(
            "INSERT INTO business (platform, name, address, latitude, longitude, category, source_url, observed_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (PLATFORM, b.name, b.address, b.latitude, b.longitude, b.category, b.source_url, observed_at),
        ).lastrowid
    else:
        conn.execute(
            "UPDATE business SET name=COALESCE(?, name), address=COALESCE(?, address),"
            " latitude=COALESCE(?, latitude), longitude=COALESCE(?, longitude),"
            " category=COALESCE(?, category), source_url=COALESCE(?, source_url), observed_at=? WHERE id=?",
            (b.name, b.address, b.latitude, b.longitude, b.category, b.source_url, observed_at, bid),
        )
    conn.execute(
        "INSERT OR IGNORE INTO business_external_id (platform, id_type, external_id, business_id, verified_by)"
        " VALUES (?,?,?,?,NULL)",
        (PLATFORM, b.id_type, b.external_id, bid),
    )
    for id_type, ext in b.verified_aliases:
        conn.execute(
            "INSERT OR IGNORE INTO business_external_id (platform, id_type, external_id, business_id, verified_by)"
            " VALUES (?,?,?,?,?)",
            (PLATFORM, id_type, ext, bid, provider),
        )
    return bid


def upsert_reviewer(conn: sqlite3.Connection, r: ReviewerRef, observed_at: str) -> int | None:
    """Accounts are keyed by stable ID only. A display name is a label: two
    accounts named alike stay two, and a rename updates the label."""
    if not r.external_id:
        return None
    row = conn.execute(
        "SELECT id FROM reviewer WHERE platform=? AND external_id=?", (PLATFORM, r.external_id)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE reviewer SET display_name=COALESCE(?, display_name), profile_url=COALESCE(?, profile_url),"
            " last_seen_at=MAX(last_seen_at, ?), first_seen_at=MIN(first_seen_at, ?) WHERE id=?",
            (r.display_name, r.profile_url, observed_at, observed_at, row[0]),
        )
        return row[0]
    return conn.execute(
        "INSERT INTO reviewer (platform, external_id, profile_url, display_name, first_seen_at, last_seen_at)"
        " VALUES (?,?,?,?,?,?)",
        (PLATFORM, r.external_id, r.profile_url, r.display_name, observed_at, observed_at),
    ).lastrowid


def review_key(rec: ReviewRecord, reviewer_id: int, business_id: int, one_review_per_pair: bool) -> str:
    if rec.external_review_id:
        return f"id:{rec.external_review_id}"
    if one_review_per_pair:
        return f"pair:{reviewer_id}:{business_id}"
    body = f"{reviewer_id}|{business_id}|{rec.published_raw}|{rec.rating}|{rec.text}"
    return "content:" + hashlib.sha256(body.encode()).hexdigest()


def store_review(
    conn: sqlite3.Connection,
    rec: ReviewRecord,
    run_id: int,
    fetched_at: str,
    provider: str,
    one_review_per_pair: bool,
) -> tuple[int, int, int, int] | None:
    """Store one observed review. Returns (review_id, observation_id,
    reviewer_id, business_id), or None when a stable ID is missing — such a
    record is excluded from identity matching, never matched by name."""
    reviewer_id = upsert_reviewer(conn, rec.reviewer, fetched_at)
    business_id = upsert_business(conn, rec.business, fetched_at, provider)
    if reviewer_id is None or business_id is None:
        return None
    published_at, precision = parse_timestamp(rec.published_raw, rec.timestamp_precision)
    key = review_key(rec, reviewer_id, business_id, one_review_per_pair)
    row = conn.execute("SELECT id FROM review WHERE platform=? AND review_key=?", (PLATFORM, key)).fetchone()
    values = (rec.rating, rec.text, rec.published_raw, published_at, precision.value, rec.source_url)
    if row:
        review_id = row[0]
        # The review row carries the latest observed content; every version
        # stays in review_observation.
        conn.execute(
            "UPDATE review SET rating=?, text=?, published_raw=?, published_at=?, timestamp_precision=?,"
            " source_url=COALESCE(?, source_url) WHERE id=?",
            (*values, review_id),
        )
    else:
        review_id = conn.execute(
            "INSERT INTO review (platform, review_key, external_review_id, reviewer_id, business_id, rating, text,"
            " published_raw, published_at, timestamp_precision, source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (PLATFORM, key, rec.external_review_id, reviewer_id, business_id, *values),
        ).lastrowid
    payload_hash = rec.payload_hash()
    conn.execute(
        "INSERT OR IGNORE INTO review_observation (review_id, run_id, fetched_at, provider, payload_hash, rating,"
        " text, published_raw, published_at, timestamp_precision, source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (review_id, run_id, fetched_at, provider, payload_hash, *values),
    )
    obs_id = conn.execute(
        "SELECT id FROM review_observation WHERE review_id=? AND run_id=? AND payload_hash=?",
        (review_id, run_id, payload_hash),
    ).fetchone()[0]
    return review_id, obs_id, reviewer_id, business_id
