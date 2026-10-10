"""Records exchanged between a provider adapter and the collector.

Everything a provider returns is untrusted input. The types here carry what
the spec requires a provider response to say (records, cursor, fetch time,
provider name, completion status) and nothing is inferred that the provider
did not say: a relative date stays relative, a missing ID stays missing.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

PLATFORM = "google_maps"


class Completion(str, enum.Enum):
    """How a provider says a paged listing ended."""

    END_REACHED = "end_reached"
    CAPPED = "capped"
    UNAVAILABLE = "unavailable"  # private, removed, or otherwise not public
    FAILED = "failed"
    UNKNOWN = "unknown"


class Precision(str, enum.Enum):
    EXACT = "exact"
    DAY = "day"
    MONTH = "month"
    RELATIVE = "relative"
    UNKNOWN = "unknown"


# Precise enough for a day-granularity timing window.
WINDOW_PRECISIONS = (Precision.EXACT.value, Precision.DAY.value)


class ProfileStatus(str, enum.Enum):
    """Outcome of one reviewer-history fetch within a run.

    Kept distinct all the way to the UI and the exports: a private profile, a
    capped one, a failed one, an empty one and one whose completeness the
    provider could not state are different facts.
    """

    PENDING = "pending"  # not attempted (yet): budget, cancel or interruption
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"  # end of history reached, at least one record
    CAPPED = "capped"  # truncated by our cap or the provider's
    EMPTY = "empty"  # end reached with no records
    UNAVAILABLE = "unavailable"  # private or not public
    FAILED = "failed"  # transient failures exhausted retries
    UNKNOWN = "unknown"  # records returned, completeness not stated

    @property
    def terminal(self) -> bool:
        return self not in (ProfileStatus.PENDING, ProfileStatus.IN_PROGRESS)


# A history counts toward E (retrieved) when at least one record came back
# and the provider did not say the profile was unavailable or failed.
USABLE_STATUSES = (ProfileStatus.COMPLETE.value, ProfileStatus.CAPPED.value, ProfileStatus.UNKNOWN.value)
PARTIAL_STATUSES = (ProfileStatus.CAPPED.value, ProfileStatus.UNKNOWN.value)
# A cached fetch with one of these outcomes can be reused within its freshness window.
CACHEABLE_STATUSES = (
    ProfileStatus.COMPLETE.value,
    ProfileStatus.CAPPED.value,
    ProfileStatus.EMPTY.value,
    ProfileStatus.UNAVAILABLE.value,
    ProfileStatus.UNKNOWN.value,
)


@dataclass(frozen=True)
class BusinessRecord:
    external_id: str | None
    id_type: str = "place_id"
    name: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    category: str | None = None
    source_url: str | None = None
    # (id_type, external_id) pairs the provider has verified name the same
    # place. Names and addresses are never merge keys.
    verified_aliases: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ReviewerRef:
    external_id: str | None
    display_name: str | None = None
    profile_url: str | None = None


@dataclass(frozen=True)
class ReviewRecord:
    reviewer: ReviewerRef
    business: BusinessRecord
    external_review_id: str | None = None
    rating: int | None = None
    text: str | None = None
    published_raw: str | None = None
    timestamp_precision: str | None = None  # provider's own statement, if any
    source_url: str | None = None
    raw: dict | None = None

    def payload_hash(self) -> str:
        """Hash of the observed content, used to tell an edit from a repeat."""
        body = {
            "review_id": self.external_review_id,
            "reviewer": self.reviewer.external_id,
            "business": self.business.external_id,
            "rating": self.rating,
            "text": self.text,
            "published": self.published_raw,
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


@dataclass
class Page:
    records: list[ReviewRecord]
    next_cursor: str | None
    fetched_at: str
    provider: str
    status: Completion | None = None  # None: more pages follow (next_cursor set)
    cost: float | None = None
    reported_total: int | None = None
    error_code: str | None = None
    raw: object = field(default=None, repr=False)


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_RELATIVE = re.compile(
    r"\bago\b|^(a|an|one|\d+)\s+(second|minute|hour|day|week|month|year)s?\b|^(yesterday|today)$",
    re.IGNORECASE,
)
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

_ORDER = [Precision.EXACT, Precision.DAY, Precision.MONTH, Precision.RELATIVE, Precision.UNKNOWN]


def parse_timestamp(raw: str | None, hint: str | None = None) -> tuple[str | None, Precision]:
    """Normalise a provider timestamp without inventing precision.

    Returns (value, precision). The value is an ISO datetime for EXACT,
    YYYY-MM-DD for DAY, YYYY-MM for MONTH and None otherwise. "Two months ago"
    is RELATIVE with no value: it never becomes a posting date. A provider hint
    can lower the precision (a midnight timestamp that is really a date) but
    never raise it.
    """
    hinted = None
    if hint:
        try:
            hinted = Precision(hint)
        except ValueError:
            hinted = Precision.UNKNOWN
    if raw is None or not str(raw).strip():
        return None, Precision.UNKNOWN
    s = str(raw).strip()

    if _RELATIVE.search(s):
        parsed: tuple[str | None, Precision] = (None, Precision.RELATIVE)
    elif _DATETIME.match(s):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None, Precision.UNKNOWN
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        parsed = (dt.astimezone(timezone.utc).isoformat(), Precision.EXACT)
    elif _DAY.match(s):
        parsed = (s, Precision.DAY)
    elif _MONTH.match(s):
        parsed = (s, Precision.MONTH)
    else:
        return None, Precision.UNKNOWN

    value, precision = parsed
    if hinted is None or _ORDER.index(hinted) <= _ORDER.index(precision):
        return value, precision
    # Coarsen to the provider's stated precision.
    if hinted == Precision.DAY and value:
        return value[:10], Precision.DAY
    if hinted == Precision.MONTH and value:
        return value[:7], Precision.MONTH
    return None, hinted
