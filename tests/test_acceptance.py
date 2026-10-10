"""Acceptance criteria from the MVP spec (§12), against the synthetic fixture.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import copy
import csv
import io
import json
import unittest

from reviewcrossover import fixture, report
from reviewcrossover.collect import Collector, CollectionError, RunLimits, create_investigation, request_cancel, start_run
from reviewcrossover.db import connect
from reviewcrossover.models import Precision, parse_timestamp
from reviewcrossover.provider import ImportProvider, TransientProviderError

SEED_URL = "https://maps.example/seed-1"


def collect(data=None, provider=None, limits=None, conn=None, inv=None):
    conn = conn or connect(":memory:")
    provider = provider or ImportProvider(data or fixture.build())
    inv = inv or create_investigation(conn, provider, SEED_URL)
    run = start_run(conn, inv, provider, limits or RunLimits())
    status = Collector(conn, provider, sleep=lambda s: None).execute(run)
    return conn, inv, run, status


def connection_for(conn, run, external_id):
    for c in report.connections(conn, run, report.Filters(min_shared=1)):
        if c["destination"]["external_id"] == external_id:
            return c
    return None


def snapshot(conn, run):
    """The normalised result of a run, independent of internal IDs."""
    conns = []
    for c in report.connections(conn, run, report.Filters(min_shared=1)):
        accounts = sorted(e["reviewer_external_id"] for e in report.evidence(conn, c["connection_id"]))
        conns.append((c["destination"]["external_id"], c["shared_reviewers"], c["retrieved_denominator"],
                      c["selected_denominator"], json.dumps(c["signals"], sort_keys=True), tuple(accounts)))
    profiles = [(p["reviewer_external_id"], p["status"], p["fetched_count"], p["capped"])
                for p in report.run_json(conn, run)["profiles"]]
    return sorted(conns), profiles


class CountingProvider(ImportProvider):
    def __init__(self, data):
        super().__init__(data)
        self.contributor_calls = 0

    def fetch_contributor_reviews(self, reviewer_external_id, cursor):
        self.contributor_calls += 1
        return super().fetch_contributor_reviews(reviewer_external_id, cursor)


class FixtureRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn, cls.inv, cls.run_id, cls.status = collect()
        cls.summary = report.run_summary(cls.conn, cls.run_id)["analysis"]

    # 1
    def test_identity_is_by_stable_id_not_display_name(self):
        rows = self.conn.execute(
            "SELECT external_id FROM reviewer WHERE display_name='Alex M.' ORDER BY external_id"
        ).fetchall()
        self.assertEqual([r[0] for r in rows], ["r000", "r050"])
        renamed = self.conn.execute("SELECT display_name FROM reviewer WHERE external_id='r002'").fetchall()
        self.assertEqual(len(renamed), 1)
        self.assertEqual(renamed[0][0], "Reviewer 2 (renamed)")

    # 2
    def test_fixture_counts_and_denominators(self):
        self.assertEqual(self.status, "completed")
        b = connection_for(self.conn, self.run_id, "dest-b")
        self.assertEqual(b["shared_reviewers"], 12)
        self.assertEqual(b["share_of_retrieved"], 0.15)
        self.assertEqual(b["share_of_selected"], 0.12)
        self.assertEqual((b["retrieved_denominator"], b["selected_denominator"]), (80, 100))
        self.assertEqual(self.summary["retrieval_coverage"], 0.8)
        self.assertEqual(self.summary["selected_reviewers"], 100)
        self.assertEqual(self.summary["retrieved_histories"], 80)
        self.assertEqual(self.summary["seed_reviews_without_reviewer_id"], 5)

    # 3
    def test_repeated_rows_and_edits_do_not_inflate(self):
        b = connection_for(self.conn, self.run_id, "dest-b")
        ev = report.evidence(self.conn, b["connection_id"])
        self.assertEqual(len({e["reviewer_external_id"] for e in ev}), 12)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM review WHERE external_review_id='dest-b:r000'").fetchone()[0], 1
        )
        versions = self.conn.execute(
            "SELECT COUNT(*) FROM review_observation o JOIN review r ON r.id=o.review_id"
            " WHERE r.external_review_id='dest-b:r001'"
        ).fetchone()[0]
        self.assertEqual(versions, 2)
        r001 = next(e for e in ev if e["reviewer_external_id"] == "r001")
        self.assertTrue(r001["dest_text"].endswith("Edited."))
        # The repeated seed row across a page boundary selects r024 once.
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM run_seed_reviewer s JOIN reviewer r ON r.id=s.reviewer_id"
                " WHERE r.external_id='r024' AND s.run_id=?", (self.run_id,)
            ).fetchone()[0], 1,
        )
        # r000: seed + dest-b + 3 solo places, despite dest-b appearing three times.
        exported = {p["reviewer_external_id"]: p for p in report.run_json(self.conn, self.run_id)["profiles"]}
        self.assertEqual(exported["r000"]["fetched_count"], 5)

    # 4
    def test_history_states_stay_distinct(self):
        self.assertEqual(self.summary["coverage"], {
            "complete": 72, "capped": 5, "unknown": 3, "empty": 4, "unavailable": 8,
            "failed": 5, "unknown_no_records": 3, "not_attempted": 0,
        })
        statuses = {p["reviewer_external_id"]: p["status"] for p in report.run_json(self.conn, self.run_id)["profiles"]}
        self.assertEqual(statuses["r080"], "unavailable")
        self.assertEqual(statuses["r088"], "failed")
        self.assertEqual(statuses["r093"], "empty")
        self.assertEqual(statuses["r075"], "unknown")
        self.assertEqual(statuses["r097"], "unknown")
        self.assertEqual(statuses["r072"], "capped")
        self.assertEqual(statuses["r078"], "capped")
        exported = {p["reviewer_external_id"]: p for p in report.run_json(self.conn, self.run_id)["profiles"]}
        self.assertEqual(exported["r078"]["fetched_count"], 100)
        self.assertEqual(exported["r097"]["fetched_count"], 0)

    # 5
    def test_imprecise_timestamps_excluded_from_window(self):
        t = connection_for(self.conn, self.run_id, "dest-b")["signals"]["timing"]
        self.assertEqual((t["within_window"], t["evaluable"], t["unavailable"]), (6, 6, 6))
        ev = report.evidence(self.conn, connection_for(self.conn, self.run_id, "dest-b")["connection_id"])
        relative = next(e for e in ev if e["reviewer_external_id"] == "r006")
        self.assertIsNone(relative["dest_published_at"])
        self.assertEqual(relative["dest_precision"], "relative")
        self.assertIsNone(relative["within_window"])

    # 6
    def test_every_count_expands_to_that_many_accounts(self):
        for c in report.connections(self.conn, self.run_id, report.Filters(min_shared=1)):
            ev = report.evidence(self.conn, c["connection_id"])
            self.assertEqual(len(ev), c["shared_reviewers"])
            self.assertEqual(len({e["reviewer_id"] for e in ev}), c["shared_reviewers"])

    # 7
    def test_common_destination_is_overlap_without_a_label(self):
        c = connection_for(self.conn, self.run_id, "dest-c")
        self.assertEqual(c["shared_reviewers"], 40)
        text = json.dumps(c).lower()
        for word in ("fraud", "fake", "suspicious", "score"):
            self.assertNotIn(word, text)
        top = report.connections(self.conn, self.run_id)
        self.assertEqual([r["destination"]["external_id"] for r in top], ["dest-c", "dest-b", "dest-e", "dest-d"])

    def test_text_and_repeated_destination_signals(self):
        b = connection_for(self.conn, self.run_id, "dest-b")["signals"]
        self.assertEqual(b["similar_text"]["accounts_flagged"], 3)
        self.assertEqual(b["cross_category"], True)
        self.assertGreater(b["distance_km"], 3900)
        groups = self.summary["repeated_destination_groups"]
        dests = {self.conn.execute(
            "SELECT external_id FROM business_external_id WHERE business_id=?", (d,)).fetchone()[0]
            for g in groups for d in g["destination_ids"]}
        self.assertIn("dest-d", dests)
        self.assertIn("dest-e", dests)

    def test_outside_sample_reviewers_not_counted(self):
        for rid in ("r100", "r104"):
            self.assertIsNone(self.conn.execute(
                "SELECT 1 FROM run_seed_reviewer s JOIN reviewer r ON r.id=s.reviewer_id WHERE r.external_id=?",
                (rid,)).fetchone())

    def test_filters_report_filtered_count_beside_total(self):
        rows = report.connections(self.conn, self.run_id, report.Filters(min_shared=1, max_rating=1))
        b = next(r for r in rows if r["destination"]["external_id"] == "dest-b")
        self.assertEqual((b["shared_reviewers_matching_filters"], b["shared_reviewers"]), (3, 12))
        near = report.connections(self.conn, self.run_id, report.Filters(min_shared=1, max_distance_km=10))
        self.assertEqual([r["destination"]["external_id"] for r in near], ["dest-c"])

    # 11
    def test_exports_reproduce_counts(self):
        rows = list(csv.DictReader(io.StringIO(report.evidence_csv(self.conn, self.run_id))))
        b = [r for r in rows if r["destination_business_id"] == "dest-b"]
        self.assertEqual(len(b), 12)
        self.assertTrue(all(r["shared_reviewers"] == "12" and r["retrieved_denominator"] == "80"
                            and r["selected_denominator"] == "100" and r["retrieval_coverage"] == "0.8" for r in b))
        self.assertTrue(all(r["destination_review_source_url"].startswith("https://") for r in b))
        self.assertIn("relative", {r["destination_timestamp_precision"] for r in b})
        data = report.run_json(self.conn, self.run_id)
        self.assertEqual(data["analysis"]["retrieved_histories"], 80)
        self.assertEqual(data["limits"]["per_profile"], 100)
        self.assertTrue(data["analysis"]["limitations"])
        jb = next(c for c in data["connections"] if c["destination"]["external_id"] == "dest-b")
        self.assertEqual(len(jb["evidence"]), jb["shared_reviewers"])
        self.assertEqual(data["analysis_version"] if "analysis_version" in data else jb["analysis_version"], "overlap-1.0")


class CollectionBehaviour(unittest.TestCase):
    # 8
    def test_resume_matches_uninterrupted(self):
        conn_a, _, run_a, _ = collect()

        class Interrupting(ImportProvider):
            calls = 0
            armed = True

            def fetch_contributor_reviews(self, rid, cursor):
                Interrupting.calls += 1
                if self.armed and Interrupting.calls == 57:
                    raise KeyboardInterrupt
                return super().fetch_contributor_reviews(rid, cursor)

        provider = Interrupting(fixture.build())
        conn_b = connect(":memory:")
        inv = create_investigation(conn_b, provider, SEED_URL)
        run_b = start_run(conn_b, inv, provider, RunLimits())
        with self.assertRaises(KeyboardInterrupt):
            Collector(conn_b, provider, sleep=lambda s: None).execute(run_b)
        self.assertEqual(conn_b.execute("SELECT status FROM run WHERE id=?", (run_b,)).fetchone()[0], "running")
        with self.assertRaises(CollectionError):
            start_run(conn_b, inv, provider, RunLimits())  # never two active runs
        provider.armed = False
        self.assertEqual(Collector(conn_b, provider, sleep=lambda s: None).execute(run_b), "completed")
        self.assertEqual(snapshot(conn_a, run_a), snapshot(conn_b, run_b))

    # 9
    def test_request_cap_stops_and_marks_partial(self):
        provider = CountingProvider(fixture.build())
        conn, _, run, status = collect(provider=provider, limits=RunLimits(max_requests=30))
        self.assertEqual(status, "partial")
        row = conn.execute("SELECT request_count, stop_reason FROM run WHERE id=?", (run,)).fetchone()
        self.assertEqual(row[0], 30)
        self.assertEqual(row[1], "request cap reached")
        self.assertEqual(provider.contributor_calls, 30 - 5)  # five seed pages first
        summary = report.run_summary(conn, run)["analysis"]
        self.assertGreater(summary["coverage"]["not_attempted"], 0)
        self.assertTrue(any("partial" in line for line in summary["limitations"]))

    def test_spend_cap(self):
        class Paid(ImportProvider):
            cost_per_request = 0.5

        conn, _, run, status = collect(provider=Paid(fixture.build()), limits=RunLimits(max_cost=10))
        self.assertEqual(status, "partial")
        self.assertLessEqual(conn.execute("SELECT cost FROM run WHERE id=?", (run,)).fetchone()[0], 10)

    def test_cancellation_stops_expansion(self):
        class Canceling(CountingProvider):
            def fetch_contributor_reviews(self, rid, cursor):
                page = super().fetch_contributor_reviews(rid, cursor)
                if self.contributor_calls == 10:
                    request_cancel(self.conn, self.run_id)
                return page

        provider = Canceling(fixture.build())
        conn = connect(":memory:")
        inv = create_investigation(conn, provider, SEED_URL)
        run = start_run(conn, inv, provider, RunLimits())
        provider.conn, provider.run_id = conn, run
        self.assertEqual(Collector(conn, provider).execute(run), "canceled")
        self.assertEqual(provider.contributor_calls, 10)
        self.assertIsNotNone(conn.execute("SELECT analysis FROM run WHERE id=?", (run,)).fetchone()[0])

    def test_unknown_cost_requires_a_cap(self):
        class NoEstimate(ImportProvider):
            def estimate_cost(self, a, b):
                return None

        provider = NoEstimate(fixture.build())
        conn = connect(":memory:")
        inv = create_investigation(conn, provider, SEED_URL)
        with self.assertRaises(CollectionError):
            start_run(conn, inv, provider, RunLimits())
        start_run(conn, inv, provider, RunLimits(max_requests=500))

    def test_transient_failures_retry_then_fail(self):
        class Flaky(ImportProvider):
            attempts: dict = {}

            def fetch_contributor_reviews(self, rid, cursor):
                n = self.attempts[rid] = self.attempts.get(rid, 0) + 1
                if rid == "r000" and n <= 2:
                    raise TransientProviderError("timeout")
                if rid == "r001":
                    raise TransientProviderError("timeout")
                return super().fetch_contributor_reviews(rid, cursor)

        provider = Flaky(fixture.build())
        conn, _, run, status = collect(provider=provider)
        statuses = {p["reviewer_external_id"]: p for p in report.run_json(conn, run)["profiles"]}
        self.assertEqual(statuses["r000"]["status"], "complete")
        self.assertEqual(statuses["r001"]["status"], "failed")
        self.assertEqual(provider.attempts["r001"], 4)  # one try and three retries
        self.assertEqual(connection_for(conn, run, "dest-b")["shared_reviewers"], 11)

    def test_fresh_cache_is_reused(self):
        provider = CountingProvider(fixture.build())
        conn, inv, run1, _ = collect(provider=provider)
        first = provider.contributor_calls
        _, _, run2, _ = collect(provider=provider, conn=conn, inv=inv)
        # Fresh histories are reused; only the five failed ones are fetched again.
        self.assertEqual(provider.contributor_calls, first + 5)
        self.assertEqual(snapshot(conn, run1)[0], snapshot(conn, run2)[0])


class Refresh(unittest.TestCase):
    # 10
    def test_partial_refetch_never_declares_deletion(self):
        data = fixture.build()
        conn, inv, run1, _ = collect(data)
        later = copy.deepcopy(data)
        later["profiles"]["r000"] = {"pages": [later["profiles"]["r000"]["pages"][0][:1]], "status": "capped"}
        later["profiles"]["r005"] = {"pages": [], "status": "failed"}
        for i in (30, 31, 32):  # three accounts newly observed at dest-d
            later["profiles"][fixture.rid(i)]["pages"][-1].append(
                fixture._review(i, "dest-d", text="Moved.", published_at="2026-10-01"))
        _, _, run2, _ = collect(later, conn=conn, inv=inv, limits=RunLimits(cache_days=0))
        diff = report.compare_runs(conn, run1, run2)
        self.assertGreater(diff["not_observed_in_later_fetch"], 0)
        self.assertEqual(set(diff["not_observed_by_later_fetch_status"]), {"capped", "failed"})
        self.assertNotIn("deleted", json.dumps({k: v for k, v in diff.items() if k != "note"}))
        self.assertEqual(diff["new_observations"], 3)
        notes = report.record_notifications(conn, run1, run2)
        self.assertEqual(len(notes), 1)
        self.assertIn("Example Moving Co", notes[0])
        self.assertIn("not proof of new reviews", notes[0])


class Units(unittest.TestCase):
    def test_timestamps_keep_their_precision(self):
        self.assertEqual(parse_timestamp("2 months ago"), (None, Precision.RELATIVE))
        self.assertEqual(parse_timestamp("a week ago"), (None, Precision.RELATIVE))
        self.assertEqual(parse_timestamp("2026-05"), ("2026-05", Precision.MONTH))
        self.assertEqual(parse_timestamp("2026-05-03"), ("2026-05-03", Precision.DAY))
        self.assertEqual(parse_timestamp("2026-05-03T10:00:00Z")[1], Precision.EXACT)
        self.assertEqual(parse_timestamp("2026-05-03T00:00:00Z", "day"), ("2026-05-03", Precision.DAY))
        self.assertEqual(parse_timestamp("2026-05-03", "exact"), ("2026-05-03", Precision.DAY))
        self.assertEqual(parse_timestamp(None), (None, Precision.UNKNOWN))

    def test_csv_formula_cells_neutralised(self):
        for v in ("=HYPERLINK(\"x\")", "+1", "-1", "@SUM(A1)"):
            self.assertTrue(report.csv_safe(v).startswith("'"))
        self.assertEqual(report.csv_safe("plain"), "plain")
        data = fixture.build()
        data["profiles"]["r003"]["pages"][1][0]["text"] = "=HYPERLINK(\"http://x\")"
        conn, _, run, _ = collect(data)
        rows = list(csv.DictReader(io.StringIO(report.evidence_csv(conn, run))))
        r3 = next(r for r in rows if r["reviewer_external_id"] == "r003" and r["destination_business_id"] == "dest-b")
        self.assertTrue(r3["destination_text"].startswith("'="))

    def test_missing_business_id_excluded_not_name_matched(self):
        data = fixture.build()
        data["profiles"]["r010"]["pages"][1][0]["business_id"] = ""
        conn, _, run, _ = collect(data)
        self.assertEqual(connection_for(conn, run, "dest-b")["shared_reviewers"], 11)
        # The record repeats across r010's page boundary, so it is seen twice.
        self.assertEqual(report.run_summary(conn, run)["analysis"]["records_without_business_id"], 2)




class CsvImport(unittest.TestCase):
    def test_example_csvs(self):
        from pathlib import Path

        from reviewcrossover.provider import dataset_from_csv

        ex = Path(__file__).resolve().parent.parent / "examples"
        data = dataset_from_csv("seed-x", ex / "seed_reviews.csv", ex / "histories.csv",
                                ex / "businesses.csv", ex / "profiles.csv")
        conn = connect(":memory:")
        provider = ImportProvider(data)
        inv = create_investigation(conn, provider, "seed-x")
        run = start_run(conn, inv, provider, RunLimits())
        self.assertEqual(Collector(conn, provider).execute(run), "completed")
        y = connection_for(conn, run, "dest-y")
        self.assertEqual((y["shared_reviewers"], y["retrieved_denominator"], y["selected_denominator"]), (3, 3, 4))
        self.assertEqual(y["signals"]["timing"], {"window_days": 7, "within_window": 2, "evaluable": 2, "unavailable": 1})


if __name__ == "__main__":
    unittest.main()
