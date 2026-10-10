"""Deterministic overlap analysis (spec §8).

S is the selected seed reviewers, E the subset with a usable retrieved
history, O(B) the accounts in E with an observed review of destination B.
The analysis reports |O(B)| with both denominators and the retrieval
coverage, and attaches supporting signals. It produces no fraud score and no
verdict: counts, explicit signals and evidence.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from itertools import combinations

from .db import transaction
from .models import USABLE_STATUSES, WINDOW_PRECISIONS
from .collect import SAMPLING_LABEL

ANALYSIS_VERSION = "overlap-1.0"

DEFAULT_PARAMS = {
    "window_days": 7,
    "text_min_chars": 80,
    "text_similarity": 0.85,
    "repeated_min_accounts": 3,
    "repeated_min_destinations": 2,
    "display_min_shared": 3,
    # Below this retrieval coverage, an empty table reads "insufficient observed data".
    "min_coverage_for_negative": 0.5,
}


# -- text ------------------------------------------------------------------

def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).casefold()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def trigrams(normalized: str) -> frozenset[str]:
    return frozenset(normalized[i : i + 3] for i in range(len(normalized) - 2))


def text_similarity(a: str | None, b: str | None, min_chars: int = 80) -> float | None:
    """Jaccard similarity of normalised character trigrams, or None when
    either text is too short (rating-only reviews included) to compare."""
    na, nb = normalize_text(a), normalize_text(b)
    if len(na) < min_chars or len(nb) < min_chars:
        return None
    ta, tb = trigrams(na), trigrams(nb)
    return len(ta & tb) / len(ta | tb)


# -- geography and time ----------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2) -> float | None:
    if None in (lat1, lon1, lat2, lon2):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * 6371.0088 * math.asin(math.sqrt(h)), 3)


def precise_date(published_at: str | None, precision: str) -> date | None:
    if precision not in WINDOW_PRECISIONS or not published_at:
        return None
    return date.fromisoformat(published_at[:10])


# -- analysis --------------------------------------------------------------

def analyze_run(conn: sqlite3.Connection, run_id: int, params: dict | None = None) -> dict:
    p = {**DEFAULT_PARAMS, **(params or {})}
    run = conn.execute(
        "SELECT r.*, i.seed_business_id FROM run r JOIN investigation i ON i.id=r.investigation_id WHERE r.id=?",
        (run_id,),
    ).fetchone()
    seed_id = run["seed_business_id"]
    seed = conn.execute("SELECT * FROM business WHERE id=?", (seed_id,)).fetchone()

    selected = conn.execute(
        "SELECT s.reviewer_id, s.seed_review_id, s.seed_observation_id, o.text, o.rating, o.published_at,"
        " o.timestamp_precision FROM run_seed_reviewer s JOIN review_observation o ON o.id=s.seed_observation_id"
        " WHERE s.run_id=? ORDER BY s.position",
        (run_id,),
    ).fetchall()
    seed_obs = {r["reviewer_id"]: r for r in selected}
    fetches = {
        r["reviewer_id"]: r
        for r in conn.execute("SELECT * FROM profile_fetch WHERE run_id=?", (run_id,)).fetchall()
    }
    usable = [
        rid for rid in seed_obs
        if rid in fetches and fetches[rid]["status"] in USABLE_STATUSES and fetches[rid]["fetched_count"] > 0
    ]

    # destination -> reviewer -> candidate destination observations
    observed: dict[int, dict[int, list[sqlite3.Row]]] = defaultdict(lambda: defaultdict(list))
    for rid in usable:
        f = fetches[rid]
        history_run = f["source_run_id"] or run_id
        for item in conn.execute(
            "SELECT rv.id AS review_id, rv.business_id, o.id AS observation_id, o.text, o.rating, o.published_at,"
            " o.timestamp_precision FROM profile_fetch_item i JOIN review rv ON rv.id=i.review_id"
            " JOIN review_observation o ON o.id=i.observation_id WHERE i.run_id=? AND i.reviewer_id=?",
            (history_run, rid),
        ):
            if item["business_id"] != seed_id:
                observed[item["business_id"]][rid].append(item)

    businesses = {
        b["id"]: b
        for b in conn.execute(
            f"SELECT * FROM business WHERE id IN ({','.join('?' * len(observed))})", tuple(observed)
        ).fetchall()
    } if observed else {}
    name_counts = Counter(normalize_text(b["name"]) for b in businesses.values() if b["name"])

    S, E = len(selected), len(usable)
    connections = []
    for dest_id, by_reviewer in observed.items():
        dest = businesses[dest_id]
        evidence = []
        for rid in sorted(by_reviewer):
            # One evidence row per account, however many rows it has here: the
            # most recent precisely dated review, then the lowest review ID.
            items = sorted(by_reviewer[rid], key=lambda o: (-_sort_date(o), o["review_id"]))
            evidence.append({"reviewer_id": rid, "seed": seed_obs[rid], "dest": items[0]})

        within = evaluable = 0
        for ev in evidence:
            d1 = precise_date(ev["seed"]["published_at"], ev["seed"]["timestamp_precision"])
            d2 = precise_date(ev["dest"]["published_at"], ev["dest"]["timestamp_precision"])
            if d1 is None or d2 is None:
                ev["within_window"] = None
                continue
            evaluable += 1
            ev["within_window"] = int(abs((d1 - d2).days) <= p["window_days"])
            within += ev["within_window"]

        seed_dest_pairs = 0
        for ev in evidence:
            sim = text_similarity(ev["seed"]["text"], ev["dest"]["text"], p["text_min_chars"])
            ev["similar_text"] = int(sim is not None and sim >= p["text_similarity"])
            seed_dest_pairs += ev["similar_text"]
        cross_pairs = 0
        for a, b in combinations(evidence, 2):
            sim = text_similarity(a["dest"]["text"], b["dest"]["text"], p["text_min_chars"])
            if sim is not None and sim >= p["text_similarity"]:
                cross_pairs += 1
                a["similar_text"] = b["similar_text"] = 1

        dest_ratings = Counter(str(ev["dest"]["rating"]) if ev["dest"]["rating"] is not None else "none" for ev in evidence)
        seed_ratings = Counter(str(ev["seed"]["rating"]) if ev["seed"]["rating"] is not None else "none" for ev in evidence)
        cross_category = None
        if seed["category"] and dest["category"]:
            cross_category = normalize_text(seed["category"]) != normalize_text(dest["category"])
        signals = {
            "distance_km": haversine_km(seed["latitude"], seed["longitude"], dest["latitude"], dest["longitude"]),
            "seed_category": seed["category"],
            "destination_category": dest["category"],
            "cross_category": cross_category,
            "timing": {
                "window_days": p["window_days"],
                "within_window": within,
                "evaluable": evaluable,
                "unavailable": len(evidence) - evaluable,
            },
            "similar_text": {
                "seed_destination_pairs": seed_dest_pairs,
                "cross_account_destination_pairs": cross_pairs,
                "accounts_flagged": sum(ev["similar_text"] for ev in evidence),
            },
            "destination_ratings": dict(sorted(dest_ratings.items())),
            "seed_ratings": dict(sorted(seed_ratings.items())),
            "other_observed_destinations_with_same_name": (
                name_counts[normalize_text(dest["name"])] - 1 if dest["name"] else None
            ),
        }
        connections.append({"destination_id": dest_id, "evidence": evidence, "signals": signals})

    groups = _repeated_groups(observed, p)
    group_membership = Counter(d for g in groups for d in g["destination_ids"])
    for c in connections:
        c["signals"]["repeated_destination_groups"] = group_membership.get(c["destination_id"], 0)

    with transaction(conn):
        conn.execute("DELETE FROM connection WHERE run_id=?", (run_id,))
        for c in connections:
            cid = conn.execute(
                "INSERT INTO connection (run_id, seed_id, destination_id, shared_count, retrieved_denominator,"
                " selected_denominator, signals, analysis_version) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, seed_id, c["destination_id"], len(c["evidence"]), E, S,
                 json.dumps(c["signals"], sort_keys=True), ANALYSIS_VERSION),
            ).lastrowid
            conn.executemany(
                "INSERT INTO connection_evidence (connection_id, reviewer_id, seed_review_id, seed_observation_id,"
                " destination_review_id, destination_observation_id, similar_text, within_window)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [
                    (cid, ev["reviewer_id"], ev["seed"]["seed_review_id"], ev["seed"]["seed_observation_id"],
                     ev["dest"]["review_id"], ev["dest"]["observation_id"], ev["similar_text"], ev["within_window"])
                    for ev in c["evidence"]
                ],
            )
        summary = _summary(conn, run, fetches, selected, usable, connections, groups, p)
        conn.execute(
            "UPDATE run SET analysis_version=?, analysis=? WHERE id=?",
            (ANALYSIS_VERSION, json.dumps(summary, sort_keys=True), run_id),
        )
    return summary


def _sort_date(o) -> int:
    d = precise_date(o["published_at"], o["timestamp_precision"])
    return d.toordinal() if d else 0


def _repeated_groups(observed, p) -> list[dict]:
    """Groups of >= N seed accounts that share >= M non-seed destinations.

    For every pair of destinations, the accounts observed at both; pairs with
    the same account set merge into one group. Conditional on the seed sample,
    like every other count here.
    """
    sets = {
        d: frozenset(by_reviewer)
        for d, by_reviewer in observed.items()
        if len(by_reviewer) >= p["repeated_min_accounts"]
    }
    by_accounts: dict[frozenset, set] = defaultdict(set)
    for a, b in combinations(sorted(sets), 2):
        shared = sets[a] & sets[b]
        if len(shared) >= p["repeated_min_accounts"]:
            by_accounts[shared].update((a, b))
    groups = [
        {"reviewer_ids": sorted(accts), "destination_ids": sorted(dests)}
        for accts, dests in by_accounts.items()
        if len(dests) >= p["repeated_min_destinations"]
    ]
    return sorted(groups, key=lambda g: (-len(g["reviewer_ids"]), -len(g["destination_ids"]), g["destination_ids"]))


def coverage_counts(fetches: dict, selected_ids) -> dict:
    counts = Counter()
    for rid in selected_ids:
        f = fetches.get(rid)
        status = f["status"] if f else "pending"
        if status in ("pending", "in_progress"):
            counts["not_attempted"] += 1
        elif status == "unknown" and not f["fetched_count"]:
            counts["unknown_no_records"] += 1
        else:
            counts[status] += 1
    keys = ("complete", "capped", "unknown", "empty", "unavailable", "failed", "unknown_no_records", "not_attempted")
    return {k: counts.get(k, 0) for k in keys}


def _summary(conn, run, fetches, selected, usable, connections, groups, p) -> dict:
    S, E = len(selected), len(usable)
    cov = coverage_counts(fetches, [r["reviewer_id"] for r in selected])
    at_threshold = sum(1 for c in connections if len(c["evidence"]) >= p["display_min_shared"])
    coverage = E / S if S else 0.0
    if at_threshold:
        finding = f"{at_threshold} destinations share at least {p['display_min_shared']} reviewers with the seed"
    elif E == 0 or coverage < p["min_coverage_for_negative"]:
        finding = "insufficient observed data"
    else:
        finding = (
            f"no destination shares {p['display_min_shared']} or more observed reviewers; "
            "this describes the observed sample, not an absence of coordination"
        )
    precisions = Counter(
        r[0] for r in conn.execute(
            "SELECT o.timestamp_precision FROM profile_fetch_item i JOIN review_observation o ON o.id=i.observation_id"
            " JOIN profile_fetch f ON f.reviewer_id=i.reviewer_id AND COALESCE(f.source_run_id, f.run_id)=i.run_id"
            " WHERE f.run_id=?",
            (run["id"],),
        )
    )
    limitations = [
        SAMPLING_LABEL.get(run["sampling_policy"], run["sampling_policy"]) + ".",
        "Observed shares count sampled seed reviewers with retrieved histories, not all customers of either business.",
        "Shared reviewers are an observed overlap, not a finding that any account is fake or that any business bought reviews.",
        "Publication dates are not visit dates; relative or month-precision dates are excluded from timing windows.",
    ]
    if E < S:
        limitations.append(f"{S - E} of {S} selected reviewers have no usable retrieved history.")
    if cov["capped"]:
        limitations.append(f"{cov['capped']} histories were capped; overlaps beyond the cap are not observed.")
    if run["status"] in ("partial", "canceled", "running", "pending"):
        limitations.append(f"Run status is {run['status']}" + (f" ({run['stop_reason']})." if run["stop_reason"] else "."))
    return {
        "analysis_version": ANALYSIS_VERSION,
        "params": p,
        "selected_reviewers": S,
        "retrieved_histories": E,
        "retrieval_coverage": round(coverage, 4),
        "coverage": cov,
        "seed_reviews_fetched": run["seed_reviews_fetched"],
        "seed_reviews_without_reviewer_id": run["seed_reviews_without_reviewer_id"],
        "records_without_business_id": run["records_without_business_id"],
        "observed_destinations": len(connections),
        "connections_at_display_threshold": at_threshold,
        "finding": finding,
        "history_timestamp_precision": dict(sorted(precisions.items())),
        "repeated_destination_groups": groups,
        "limitations": limitations,
    }
