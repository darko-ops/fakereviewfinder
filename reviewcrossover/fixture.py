"""A deterministic synthetic dataset with known answers.

Every business and account in it is invented. It is the fixture behind the
acceptance tests and the example in the README:

* 105 seed reviews over 5 pages; 5 have no reviewer ID; one repeats across a
  page boundary. The first 100 distinct reviewers (r000-r099) are selected;
  r100-r104 are not.
* Of those 100, 80 histories are retrieved (r000-r079): 72 complete, 3 capped
  by the provider, 2 longer than the 100-record cap, 3 with completeness
  unknown. Of the other 20: 8 unavailable, 5 failed, 4 empty, 3 never
  reported (unknown, no records).
* Destination "dest-b" is reviewed by exactly r000-r011: 12 shared reviewers,
  15% of retrieved, 12% of selected, 80% coverage. r000's review of it repeats
  across a page boundary and r001's is edited between pages; r002 is renamed
  between seed and history; r000 and r050 share a display name.
  Dates: r000-r005 exact and within 3 days of their seed review, r006-r008
  relative ("2 months ago"), r009-r011 month precision. r000-r002 wrote
  near-identical long texts.
* "dest-c" is a busy local market reviewed by 40 of the retrieved accounts.
* "dest-d" and "dest-e" are both reviewed by r020-r023 (a repeated-destination
  group of four accounts and two destinations).
"""

from __future__ import annotations

SEED = "seed-1"

BUSINESSES = {
    SEED: {"name": "Harbor Example Fish House", "address": "1 Pier Rd, Exampleton", "latitude": 40.0,
           "longitude": -73.0, "category": "Seafood restaurant", "source_url": "https://maps.example/seed-1"},
    "dest-b": {"name": "Distant Example Dental", "address": "9 Far St, Otherville", "latitude": 34.05,
               "longitude": -118.25, "category": "Dentist", "source_url": "https://maps.example/dest-b"},
    "dest-c": {"name": "Exampleton Market", "address": "2 Main St, Exampleton", "latitude": 40.01,
               "longitude": -73.01, "category": "Grocery store", "source_url": "https://maps.example/dest-c"},
    "dest-d": {"name": "Example Moving Co", "address": "5 Elm St, Thirdtown", "latitude": 41.0,
               "longitude": -74.0, "category": "Moving company", "source_url": "https://maps.example/dest-d"},
    "dest-e": {"name": "Example Locksmith", "address": "6 Oak St, Thirdtown", "latitude": 41.01,
               "longitude": -74.01, "category": "Locksmith", "source_url": "https://maps.example/dest-e"},
}

TEMPLATE = (
    "Absolutely the best experience I have had in years, the staff were friendly, professional and quick. "
    "Highly recommend to anyone looking for quality service at a fair price!"
)


def rid(i: int) -> str:
    return f"r{i:03d}"


def _name(i: int) -> str:
    if i in (0, 50):
        return "Alex M."
    return f"Reviewer {i}"


def _seed_day(i: int) -> str:
    return f"2026-08-{(i % 28) + 1:02d}"


def _review(i: int, business: str, **kw) -> dict:
    r = {
        "review_id": f"{business}:{rid(i)}",
        "reviewer_id": rid(i),
        "reviewer_name": _name(i),
        "profile_url": f"https://maps.example/contrib/{rid(i)}",
        "business_id": business,
        "rating": 5,
        "text": "",
        "published_at": _seed_day(i),
        "source_url": f"https://maps.example/review/{business}/{rid(i)}",
    }
    r.update(kw)
    return r


def _seed_review(i: int) -> dict:
    return _review(i, SEED, text=f"Good fish, review number {i}.")


def _dest_b(i: int) -> dict:
    if i <= 5:
        day = int(_seed_day(i)[-2:])
        kw = {"published_at": f"2026-08-{min(day + 3, 31):02d}T12:00:00Z"}
    elif i <= 8:
        kw = {"published_at": "2 months ago", "timestamp_precision": "relative"}
    else:
        kw = {"published_at": "2026-05"}
    text = TEMPLATE + (" Ten stars." if i == 1 else "") if i <= 2 else f"Fine dentist, visit {i}."
    return _review(i, "dest-b", text=text, rating=5 if i % 4 else 1, **kw)


def build() -> dict:
    seed_rows = []
    for i in range(105):
        seed_rows.append(_seed_review(i))
        if i in (10, 40, 70, 90, 95):
            seed_rows.append(_review(1000 + i, SEED, reviewer_id=None, review_id=f"anon-{i}", reviewer_name="A Google User"))
    # r024's review is repeated at the start of the next page.
    pages = [seed_rows[0:25], seed_rows[24:50], seed_rows[50:75], seed_rows[75:100], seed_rows[100:]]

    profiles: dict[str, dict] = {}
    for i in range(80):
        history = [_seed_review(i)]
        if i < 12:
            history.append(_dest_b(i))
        if 20 <= i < 60:
            history.append(_review(i, "dest-c", text="Fresh produce.", rating=4, published_at="2026-03-01"))
        if 20 <= i < 24:
            history.append(_review(i, "dest-d", text="Moved us fine.", published_at="2026-07-01"))
            history.append(_review(i, "dest-e", text="Opened the door.", published_at="2026-07-02"))
        for k in range(3):
            history.append(_review(i, f"solo-{i}-{k}", text="", published_at="2025-01-01",
                                   business_name=f"Solo place {i}-{k}"))
        if i in (78, 79):  # long histories: the 100-record cap applies
            for k in range(130):
                history.append(_review(i, f"long-{i}-{k}", published_at="2024-01-01",
                                       business_name=f"Long tail {i}-{k}"))
        if i == 2:  # renamed since the seed review was listed
            history = [{**r, "reviewer_name": "Reviewer 2 (renamed)"} for r in history]
        # Two pages; r000's dest-b review repeats across the boundary and
        # r001's is edited on the second page.
        split = 2
        page1, page2 = history[:split], history[split - 1 :]
        if i == 0:
            page2 = [history[1]] + page2
        if i == 1:
            page2 = [{**history[1], "text": history[1]["text"] + " Edited."}] + page2[1:]
        if 72 <= i < 75:
            status = "capped"
        elif 75 <= i < 78:
            status = "unknown"
        else:
            status = "end_reached"
        profiles[rid(i)] = {"pages": [page1, page2], "status": status, "reported_total": len(history)}
    for i in range(80, 88):
        profiles[rid(i)] = {"pages": [], "status": "unavailable", "error_code": "private"}
    for i in range(88, 93):
        profiles[rid(i)] = {"pages": [], "status": "failed", "error_code": "provider_500"}
    for i in range(93, 97):
        profiles[rid(i)] = {"pages": [[]], "status": "end_reached"}
    # r097-r099: absent, reported as unknown with no records.
    # r100-r104 review dest-b too, but are outside the sample.
    for i in range(100, 105):
        profiles[rid(i)] = {"pages": [[_seed_review(i), _review(i, "dest-b", text="Outside the sample.")]],
                            "status": "end_reached"}

    businesses = dict(BUSINESSES)
    return {
        "provider": "fixture",
        "one_review_per_pair": False,
        "businesses": businesses,
        "resolve": {"https://maps.example/seed-1": SEED},
        "business_reviews": {SEED: {"pages": pages, "status": "end_reached"}},
        "profiles": profiles,
    }


if __name__ == "__main__":
    import json
    import sys

    json.dump(build(), sys.stdout, indent=1)
