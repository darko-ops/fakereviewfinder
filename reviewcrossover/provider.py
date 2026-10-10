"""The provider adapter contract and the import-based fallback provider.

No collection vendor is selected (spec §5). A vendor adapter implements
`Provider`; until one passes the Milestone 1 pilot, `ImportProvider` serves
analyst-supplied JSON (or CSV converted to it) through the same contract, so
the collector, the analysis and the exports do not change when a vendor does.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Protocol

from .models import BusinessRecord, Completion, Page, ReviewerRef, ReviewRecord, utcnow


class ProviderError(Exception):
    code = "provider_error"

    def __init__(self, message: str = "", code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class TransientProviderError(ProviderError):
    """Worth retrying: timeouts, rate limiting, 5xx."""

    code = "transient"


class AuthorizationError(ProviderError):
    """Credentials refused. Stops the run."""

    code = "authorization"


class AccessRestrictedError(ProviderError):
    """The provider refuses this kind of access. Stops the run."""

    code = "access_restricted"


class Provider(Protocol):
    name: str
    # True only when the provider's documented semantics establish one current
    # review per reviewer/business pair; it makes that pair a review key when a
    # review ID is absent.
    one_review_per_pair: bool
    # Expected cost of one request, or None when unknown.
    cost_per_request: float | None

    def resolve_business(self, ref: str) -> BusinessRecord: ...

    def fetch_business_reviews(self, business_external_id: str, cursor: str | None) -> Page: ...

    def fetch_contributor_reviews(self, reviewer_external_id: str, cursor: str | None) -> Page: ...

    def estimate_cost(self, seed_reviewers: int, per_profile: int) -> float | None: ...


class ImportProvider:
    """Serves a dataset file through the provider contract.

    Dataset format (JSON)::

        {
          "provider": "import",
          "one_review_per_pair": false,
          "businesses": {"<id>": {"name": ..., "address": ..., "latitude": ...,
                                  "longitude": ..., "category": ...,
                                  "source_url": ..., "id_type": "place_id"}},
          "resolve": {"<url or ref>": "<business id>"},
          "business_reviews": {"<business id>": {"pages": [[record, ...], ...],
                                                 "status": "end_reached"}},
          "profiles": {"<reviewer id>": {"pages": [[record, ...], ...],
                                         "status": "end_reached|capped|unavailable|failed|unknown",
                                         "reported_total": 123}}
        }

    A record is ``{"review_id", "reviewer_id", "reviewer_name", "profile_url",
    "business_id", "rating", "text", "published_at", "timestamp_precision",
    "source_url"}``; business fields may also be given inline. A reviewer absent
    from ``profiles`` is reported as status ``unknown`` with no records: the
    import did not say whether the history exists.
    """

    cost_per_request = 0.0

    def __init__(self, data: dict, name: str | None = None):
        self.data = data
        self.name = name or data.get("provider", "import")
        self.one_review_per_pair = bool(data.get("one_review_per_pair", False))
        self.businesses: dict = data.get("businesses", {})

    @classmethod
    def from_path(cls, path: str | Path) -> "ImportProvider":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    # -- contract ---------------------------------------------------------

    def resolve_business(self, ref: str) -> BusinessRecord:
        ext = self.data.get("resolve", {}).get(ref, ref)
        if ext not in self.businesses:
            raise ProviderError(f"business not found in import: {ref}", code="not_found")
        return self._business(ext)

    def fetch_business_reviews(self, business_external_id: str, cursor: str | None) -> Page:
        listing = self.data.get("business_reviews", {}).get(business_external_id)
        if listing is None:
            return Page([], None, utcnow(), self.name, Completion.UNAVAILABLE)
        return self._page(listing, cursor, default_reviewer=None)

    def fetch_contributor_reviews(self, reviewer_external_id: str, cursor: str | None) -> Page:
        listing = self.data.get("profiles", {}).get(reviewer_external_id)
        if listing is None:
            return Page([], None, utcnow(), self.name, Completion.UNKNOWN)
        return self._page(listing, cursor, default_reviewer=reviewer_external_id)

    def estimate_cost(self, seed_reviewers: int, per_profile: int) -> float | None:
        return 0.0

    # -- helpers ----------------------------------------------------------

    def _page(self, listing: dict, cursor: str | None, default_reviewer: str | None) -> Page:
        pages = listing.get("pages", [])
        idx = int(cursor) if cursor else 0
        status = Completion(listing.get("status", Completion.END_REACHED.value))
        if status in (Completion.UNAVAILABLE, Completion.FAILED) and not pages:
            return Page([], None, utcnow(), self.name, status, error_code=listing.get("error_code"))
        rows = pages[idx] if idx < len(pages) else []
        last = idx + 1 >= len(pages)
        records = [self._record(r, default_reviewer) for r in rows]
        return Page(
            records=records,
            next_cursor=None if last else str(idx + 1),
            fetched_at=utcnow(),
            provider=self.name,
            status=status if last else None,
            reported_total=listing.get("reported_total"),
            raw=rows,
        )

    def _business(self, ext: str | None, inline: dict | None = None) -> BusinessRecord:
        info = dict(self.businesses.get(ext, {})) if ext else {}
        for key, value in (inline or {}).items():
            if value not in (None, ""):
                info.setdefault(key, value)
        aliases = tuple(tuple(a) for a in info.get("verified_aliases", []))
        return BusinessRecord(
            external_id=ext,
            id_type=info.get("id_type", "place_id"),
            name=info.get("name"),
            address=info.get("address"),
            latitude=_float(info.get("latitude")),
            longitude=_float(info.get("longitude")),
            category=info.get("category"),
            source_url=info.get("source_url"),
            verified_aliases=aliases,
        )

    def _record(self, r: dict, default_reviewer: str | None) -> ReviewRecord:
        inline = {
            k: r.get(f"business_{k}")
            for k in ("name", "address", "latitude", "longitude", "category", "source_url")
        }
        return ReviewRecord(
            reviewer=ReviewerRef(
                external_id=r.get("reviewer_id") or default_reviewer,
                display_name=r.get("reviewer_name"),
                profile_url=r.get("profile_url"),
            ),
            business=self._business(r.get("business_id") or None, inline),
            external_review_id=r.get("review_id") or None,
            rating=_int(r.get("rating")),
            text=r.get("text"),
            published_raw=r.get("published_at"),
            timestamp_precision=r.get("timestamp_precision") or None,
            source_url=r.get("source_url"),
            raw=r,
        )


def _float(v) -> float | None:
    if v in (None, ""):
        return None
    return float(v)


def _int(v) -> int | None:
    if v in (None, ""):
        return None
    return int(float(v))


CSV_BUSINESS_FIELDS = ("business_id", "id_type", "name", "address", "latitude", "longitude", "category", "source_url")


def dataset_from_csv(
    seed_business: str,
    seed_reviews: str | Path,
    histories: str | Path,
    businesses: str | Path | None = None,
    profiles: str | Path | None = None,
) -> dict:
    """Build an import dataset from CSV files.

    ``seed_reviews`` and ``histories`` are review rows (the record fields
    above). ``businesses`` has CSV_BUSINESS_FIELDS. ``profiles`` has
    ``reviewer_id,status[,reported_total]``; a reviewer with history rows but
    no stated status is ``unknown``, since a hand export rarely shows whether it
    reached the end of a profile.
    """
    def rows(path):
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    data: dict = {"provider": "import-csv", "businesses": {}, "business_reviews": {}, "profiles": {}}
    if businesses:
        for b in rows(businesses):
            bid = b.pop("business_id")
            data["businesses"][bid] = {k: v for k, v in b.items() if v not in (None, "")}
    data["business_reviews"][seed_business] = {"pages": [rows(seed_reviews)], "status": "end_reached"}
    data["businesses"].setdefault(seed_business, {})

    by_reviewer: dict[str, list] = {}
    for r in rows(histories):
        by_reviewer.setdefault(r["reviewer_id"], []).append(r)
        if r.get("business_id"):
            data["businesses"].setdefault(r["business_id"], {})
    statuses = {}
    if profiles:
        for p in rows(profiles):
            statuses[p["reviewer_id"]] = p
    for rid in sorted(set(by_reviewer) | set(statuses)):
        st = statuses.get(rid, {})
        entry = {"pages": [by_reviewer.get(rid, [])] if rid in by_reviewer else [], "status": st.get("status") or "unknown"}
        if st.get("reported_total"):
            entry["reported_total"] = int(st["reported_total"])
        data["profiles"][rid] = entry
    return data
