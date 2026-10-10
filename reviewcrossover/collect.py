"""Bounded collection: seed reviews, a sample of seed reviewers, their histories.

One outward expansion only: seed business -> seed reviewers -> the businesses
in their histories. Investigating a discovered business is a separate run.

Every provider page is written in one transaction together with the cursor
that follows it, so a run interrupted at any point resumes from the last
stored page and, given the same provider responses, normalises to the same
result as an uninterrupted run (acceptance criterion 8).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .db import transaction
from .models import CACHEABLE_STATUSES, Completion, Page, ProfileStatus, utcnow
from .provider import AccessRestrictedError, AuthorizationError, Provider, ProviderError, TransientProviderError
from .store import store_review, upsert_business

SAMPLING_NEWEST = "newest_distinct_reviewers"
SAMPLING_LABEL = {
    SAMPLING_NEWEST: "Newest distinct reviewers in provider order (recency-biased sample)",
}

ACTIVE_RUN_STATUSES = ("pending", "running")


@dataclass
class RunLimits:
    sample_size: int = 100
    per_profile: int = 100
    max_seed_reviews: int = 1000
    max_requests: int | None = None
    max_cost: float | None = None
    cache_days: float = 7
    max_retries: int = 3
    backoff_seconds: float = 1.0
    keep_raw_payloads: bool = True


class CollectionError(Exception):
    pass


class _Stop(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def create_investigation(
    conn: sqlite3.Connection, provider: Provider, business_ref: str, owner_id: str = "local", settings: dict | None = None
) -> int:
    business = provider.resolve_business(business_ref)
    now = utcnow()
    with transaction(conn):
        bid = upsert_business(conn, business, now, provider.name)
        if bid is None:
            raise CollectionError("the provider did not return a stable business ID")
        return conn.execute(
            "INSERT INTO investigation (seed_business_id, owner_id, settings, refresh_schedule, created_at)"
            " VALUES (?,?,?,NULL,?)",
            (bid, owner_id, json.dumps(settings or {}), now),
        ).lastrowid


def start_run(
    conn: sqlite3.Connection,
    investigation_id: int,
    provider: Provider,
    limits: RunLimits,
    sampling_policy: str = SAMPLING_NEWEST,
) -> int:
    """Create a run. Refuses while another run of the investigation is active,
    and refuses an unbounded run when the provider cannot estimate cost."""
    if sampling_policy not in SAMPLING_LABEL:
        raise CollectionError(f"unknown sampling policy {sampling_policy}")
    estimate = provider.estimate_cost(limits.sample_size, limits.per_profile)
    if estimate is None and limits.max_requests is None and limits.max_cost is None:
        raise CollectionError("cost estimate unavailable: set a request cap or a spend cap")
    with transaction(conn):
        active = conn.execute(
            f"SELECT id FROM run WHERE investigation_id=? AND status IN ({','.join('?' * len(ACTIVE_RUN_STATUSES))})",
            (investigation_id, *ACTIVE_RUN_STATUSES),
        ).fetchone()
        if active:
            raise CollectionError(f"run {active[0]} is still active for this investigation: resume or cancel it")
        return conn.execute(
            "INSERT INTO run (investigation_id, provider, sampling_policy, limits, status, cost_estimate)"
            " VALUES (?,?,?,?,'pending',?)",
            (investigation_id, provider.name, sampling_policy, json.dumps(asdict(limits)), estimate),
        ).lastrowid


def request_cancel(conn: sqlite3.Connection, run_id: int, interrupted: bool = False) -> None:
    """Stop further requests and job expansion at the next check.

    A run that never started is canceled straight away. `interrupted` says the
    run is not executing anywhere (its process died), so it is closed now and
    its partial data analysed; otherwise the executing collector stops itself.
    """
    with transaction(conn):
        conn.execute("UPDATE run SET cancel_requested=1 WHERE id=?", (run_id,))
        status = conn.execute("SELECT status FROM run WHERE id=?", (run_id,)).fetchone()[0]
        close = status == "pending" or (interrupted and status == "running")
        if close:
            conn.execute(
                "UPDATE run SET status='canceled', stop_reason='canceled by user', finished_at=? WHERE id=?",
                (utcnow(), run_id),
            )
    if close and status == "running":
        from .analyze import analyze_run

        analyze_run(conn, run_id)


class Collector:
    def __init__(
        self,
        conn: sqlite3.Connection,
        provider: Provider,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.conn = conn
        self.provider = provider
        self.sleep = sleep
        self.now = now

    # -- public -----------------------------------------------------------

    def execute(self, run_id: int) -> str:
        """Run (or resume) a collection. Returns the final run status.

        An exception that is not a provider error (a crash, Ctrl-C) leaves the
        run 'running' with its cursors stored; calling execute again resumes.
        """
        run = self._run(run_id)
        if run["status"] in ("completed", "partial", "failed", "canceled"):
            return run["status"]
        if run["provider"] != self.provider.name:
            raise CollectionError(f"run {run_id} was collected with {run['provider']}, not {self.provider.name}")
        self.limits = RunLimits(**json.loads(run["limits"]))
        self.run_id = run_id
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE run SET status='running', started_at=COALESCE(started_at, ?) WHERE id=?", (utcnow(), run_id)
            )
        try:
            self._collect_seed()
            self._collect_profiles()
            status, reason = "completed", None
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM profile_fetch WHERE run_id=? AND status IN ('pending','in_progress')", (run_id,)
            ).fetchone()[0]
            if pending:
                status, reason = "partial", "profiles not attempted"
        except _Stop as stop:
            status, reason = stop.status, stop.reason
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE run SET status=?, stop_reason=?, finished_at=? WHERE id=?", (status, reason, utcnow(), run_id)
            )
        from .analyze import analyze_run

        if status != "failed" or self._selected_count():
            analyze_run(self.conn, run_id)
        return status

    # -- seed -------------------------------------------------------------

    def _collect_seed(self) -> None:
        run = self._run(self.run_id)
        if run["seed_done"]:
            return
        seed = self.conn.execute(
            "SELECT b.id, e.external_id FROM investigation i JOIN business b ON b.id=i.seed_business_id"
            " JOIN business_external_id e ON e.business_id=b.id AND e.verified_by IS NULL"
            " WHERE i.id=? ORDER BY e.rowid LIMIT 1",
            (run["investigation_id"],),
        ).fetchone()
        cursor = run["seed_cursor"]
        while True:
            self._check_cancel_and_budget()
            try:
                page = self._call(lambda: self.provider.fetch_business_reviews(seed["external_id"], cursor))
            except _Stop:
                raise
            except TransientProviderError as e:
                raise _Stop("failed", f"seed reviews failed after retries: {e.code}")
            status = page.status
            if status in (Completion.UNAVAILABLE, Completion.FAILED):
                raise _Stop("failed", f"seed reviews {status.value}")
            with transaction(self.conn):
                self._store_payload(page, f"business_reviews:{seed['external_id']}:{cursor}")
                selected = self._selected_count()
                fetched = 0
                for rec in page.records:
                    if self._run(self.run_id)["seed_reviews_fetched"] + fetched >= self.limits.max_seed_reviews:
                        break
                    fetched += 1
                    stored = store_review(
                        self.conn, rec, self.run_id, page.fetched_at, self.provider.name,
                        self.provider.one_review_per_pair,
                    )
                    if stored is None:
                        col = "seed_reviews_without_reviewer_id" if not rec.reviewer.external_id else "records_without_business_id"
                        self.conn.execute(f"UPDATE run SET {col}={col}+1 WHERE id=?", (self.run_id,))
                        continue
                    review_id, obs_id, reviewer_id, business_id = stored
                    if business_id != seed["id"] or selected >= self.limits.sample_size:
                        continue
                    added = self.conn.execute(
                        "INSERT OR IGNORE INTO run_seed_reviewer (run_id, reviewer_id, position, seed_review_id,"
                        " seed_observation_id) VALUES (?,?,?,?,?)",
                        (self.run_id, reviewer_id, selected, review_id, obs_id),
                    ).rowcount
                    if added:
                        selected += 1
                        self.conn.execute(
                            "INSERT INTO profile_fetch (run_id, reviewer_id, status) VALUES (?,?,'pending')",
                            (self.run_id, reviewer_id),
                        )
                cursor = page.next_cursor
                done = (
                    cursor is None
                    or selected >= self.limits.sample_size
                    or self._run(self.run_id)["seed_reviews_fetched"] + fetched >= self.limits.max_seed_reviews
                )
                self.conn.execute(
                    "UPDATE run SET seed_cursor=?, seed_done=?, seed_status=?,"
                    " seed_reviews_fetched=seed_reviews_fetched+? WHERE id=?",
                    (cursor, int(done), status.value if status else ("stopped_at_limit" if done else None), fetched, self.run_id),
                )
            if done:
                return

    # -- profiles ---------------------------------------------------------

    def _collect_profiles(self) -> None:
        rows = self.conn.execute(
            "SELECT s.reviewer_id, r.external_id, p.status, p.cursor FROM run_seed_reviewer s"
            " JOIN reviewer r ON r.id=s.reviewer_id JOIN profile_fetch p ON p.run_id=s.run_id AND p.reviewer_id=s.reviewer_id"
            " WHERE s.run_id=? ORDER BY s.position",
            (self.run_id,),
        ).fetchall()
        for row in rows:
            if ProfileStatus(row["status"]).terminal:
                continue
            if row["status"] == "pending" and self._reuse_cached(row["reviewer_id"]):
                continue
            self._collect_profile(row["reviewer_id"], row["external_id"], row["cursor"])

    def _reuse_cached(self, reviewer_id: int) -> bool:
        if self.limits.cache_days <= 0:
            return False
        cutoff = (self.now() - timedelta(days=self.limits.cache_days)).replace(microsecond=0).isoformat()
        placeholders = ",".join("?" * len(CACHEABLE_STATUSES))
        cached = self.conn.execute(
            f"SELECT p.*, r.limits, r.provider FROM profile_fetch p JOIN run r ON r.id=p.run_id"
            f" WHERE p.reviewer_id=? AND p.run_id<>? AND p.source_run_id IS NULL AND p.status IN ({placeholders})"
            f" AND p.fetched_at >= ? AND r.provider=? ORDER BY p.fetched_at DESC LIMIT 1",
            (reviewer_id, self.run_id, *CACHEABLE_STATUSES, cutoff, self.provider.name),
        ).fetchone()
        if cached is None:
            return False
        # Reuse only a fetch this run's cap would have produced too: one made
        # under the same cap, or one that ended within this cap.
        same_cap = json.loads(cached["limits"])["per_profile"] == self.limits.per_profile
        if not same_cap and (cached["capped"] or cached["fetched_count"] > self.limits.per_profile):
            return False
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE profile_fetch SET status=?, fetched_count=?, reported_total=?, capped=?, error_code=?,"
                " fetched_at=?, source_run_id=? WHERE run_id=? AND reviewer_id=?",
                (cached["status"], cached["fetched_count"], cached["reported_total"], cached["capped"],
                 cached["error_code"], cached["fetched_at"], cached["run_id"], self.run_id, reviewer_id),
            )
        return True

    def _collect_profile(self, reviewer_id: int, external_id: str, cursor: str | None) -> None:
        while True:
            self._check_cancel_and_budget()
            try:
                page = self._call(lambda: self.provider.fetch_contributor_reviews(external_id, cursor))
            except TransientProviderError as e:
                with transaction(self.conn):
                    self._finish_profile(reviewer_id, ProfileStatus.FAILED, error_code=e.code)
                return
            with transaction(self.conn):
                self._store_payload(page, f"contributor_reviews:{external_id}:{cursor}")
                count = self._fetched_count(reviewer_id)
                capped = False
                for rec in page.records:
                    if count >= self.limits.per_profile:
                        capped = True
                        break
                    if rec.reviewer.external_id and rec.reviewer.external_id != external_id:
                        continue  # a record for another account: not this history
                    stored = store_review(
                        self.conn, rec, self.run_id, page.fetched_at, self.provider.name,
                        self.provider.one_review_per_pair,
                    )
                    if stored is None:
                        self.conn.execute(
                            "UPDATE run SET records_without_business_id=records_without_business_id+1 WHERE id=?",
                            (self.run_id,),
                        )
                        continue
                    review_id, obs_id, _, _ = stored
                    self.conn.execute(
                        "INSERT INTO profile_fetch_item (run_id, reviewer_id, review_id, observation_id)"
                        " VALUES (?,?,?,?) ON CONFLICT (run_id, reviewer_id, review_id)"
                        " DO UPDATE SET observation_id=excluded.observation_id",
                        (self.run_id, reviewer_id, review_id, obs_id),
                    )
                    count = self._fetched_count(reviewer_id)
                if count >= self.limits.per_profile and page.next_cursor is not None:
                    capped = True
                cursor = page.next_cursor
                self.conn.execute(
                    "UPDATE profile_fetch SET status='in_progress', cursor=?, fetched_count=?,"
                    " reported_total=COALESCE(?, reported_total), fetched_at=? WHERE run_id=? AND reviewer_id=?",
                    (cursor, count, page.reported_total, page.fetched_at, self.run_id, reviewer_id),
                )
                if capped or page.status == Completion.CAPPED:
                    self._finish_profile(reviewer_id, ProfileStatus.CAPPED, capped=True)
                    return
                if page.status is not None or cursor is None:
                    self._finish_profile(reviewer_id, _profile_status(page.status, count), error_code=page.error_code)
                    return

    def _finish_profile(self, reviewer_id: int, status: ProfileStatus, capped: bool = False, error_code: str | None = None):
        self.conn.execute(
            "UPDATE profile_fetch SET status=?, capped=?, error_code=?, fetched_at=COALESCE(fetched_at, ?)"
            " WHERE run_id=? AND reviewer_id=?",
            (status.value, int(capped), error_code, utcnow(), self.run_id, reviewer_id),
        )

    # -- provider calls ---------------------------------------------------

    def _call(self, fn: Callable[[], Page]) -> Page:
        """One logical request with bounded retries. Every attempt is counted
        against the request and spend caps."""
        attempt = 0
        while True:
            self._charge()
            try:
                page = fn()
            except TransientProviderError:
                attempt += 1
                if attempt > self.limits.max_retries:
                    raise
                self.sleep(self.limits.backoff_seconds * (2 ** (attempt - 1)))
                self._check_cancel_and_budget()
                continue
            except (AuthorizationError, AccessRestrictedError) as e:
                raise _Stop("failed" if not self._usable_count() else "partial", e.code)
            except ProviderError as e:
                raise _Stop("failed" if not self._usable_count() else "partial", e.code)
            if page.cost is not None:
                expected = self.provider.cost_per_request or 0.0
                with transaction(self.conn):
                    self.conn.execute("UPDATE run SET cost=cost+? WHERE id=?", (page.cost - expected, self.run_id))
            return page

    def _charge(self) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE run SET request_count=request_count+1, cost=cost+? WHERE id=?",
                (self.provider.cost_per_request or 0.0, self.run_id),
            )

    def _check_cancel_and_budget(self) -> None:
        run = self._run(self.run_id)
        if run["cancel_requested"]:
            raise _Stop("canceled", "canceled by user")
        if self.limits.max_requests is not None and run["request_count"] >= self.limits.max_requests:
            raise _Stop("partial", "request cap reached")
        next_cost = self.provider.cost_per_request or 0.0
        if self.limits.max_cost is not None and run["cost"] + next_cost > self.limits.max_cost:
            raise _Stop("partial", "spend cap reached")

    # -- helpers ----------------------------------------------------------

    def _store_payload(self, page: Page, request: str) -> None:
        if self.limits.keep_raw_payloads and page.raw is not None:
            self.conn.execute(
                "INSERT INTO raw_payload (run_id, provider, request, fetched_at, payload) VALUES (?,?,?,?,?)",
                (self.run_id, page.provider, request, page.fetched_at, json.dumps(page.raw, default=str)),
            )

    def _run(self, run_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()

    def _selected_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM run_seed_reviewer WHERE run_id=?", (self.run_id,)).fetchone()[0]

    def _fetched_count(self, reviewer_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM profile_fetch_item WHERE run_id=? AND reviewer_id=?", (self.run_id, reviewer_id)
        ).fetchone()[0]

    def _usable_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM profile_fetch WHERE run_id=? AND fetched_count>0", (self.run_id,)
        ).fetchone()[0]


def _profile_status(status: Completion | None, count: int) -> ProfileStatus:
    if status in (None, Completion.END_REACHED):
        return ProfileStatus.COMPLETE if count else ProfileStatus.EMPTY
    if status == Completion.UNAVAILABLE:
        return ProfileStatus.UNAVAILABLE
    if status == Completion.FAILED:
        return ProfileStatus.FAILED
    return ProfileStatus.UNKNOWN
