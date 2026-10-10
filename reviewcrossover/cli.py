"""Command line: python -m reviewcrossover <command> --db crossover.db ..."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import collect, maintenance, report
from .analyze import analyze_run
from .db import connect
from .provider import ImportProvider, ProviderError, dataset_from_csv


def _provider(spec: str):
    kind, _, arg = spec.partition(":")
    if kind == "import" and arg:
        return ImportProvider.from_path(arg)
    raise SystemExit(
        f"unknown provider {spec!r}. Only 'import:<dataset.json>' exists until a vendor passes the collection pilot."
    )


def _limits(a) -> collect.RunLimits:
    return collect.RunLimits(
        sample_size=a.sample, per_profile=a.per_profile, max_seed_reviews=a.max_seed_reviews,
        max_requests=a.max_requests, max_cost=a.max_cost, cache_days=a.cache_days,
    )


def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, sort_keys=False, default=str)
    sys.stdout.write("\n")


def _text(s: str | None, width: int = 0) -> str:
    """Untrusted review/business text for a terminal: no control characters."""
    s = "".join(ch if ch.isprintable() else " " for ch in (s or ""))
    return s if not width or len(s) <= width else s[: width - 1] + "…"


def cmd_resolve(a):
    b = _provider(a.provider).resolve_business(a.business)
    _print_json({"external_id": b.external_id, "id_type": b.id_type, "name": b.name, "address": b.address,
                 "category": b.category, "source_url": b.source_url})


def cmd_new(a):
    provider = _provider(a.provider)
    b = provider.resolve_business(a.business)
    print(f"Resolved: {_text(b.name)} — {_text(b.address)} [{b.id_type} {b.external_id}]")
    if not a.yes:
        if input("Start an investigation of this business? [y/N] ").strip().lower() != "y":
            raise SystemExit("not confirmed")
    conn = connect(a.db)
    inv = collect.create_investigation(conn, provider, a.business, owner_id=a.owner)
    print(f"investigation {inv}")


def cmd_run(a):
    conn = connect(a.db)
    provider = _provider(a.provider)
    limits = _limits(a)
    estimate = provider.estimate_cost(limits.sample_size, limits.per_profile)
    print("estimated cost: " + ("estimate unavailable" if estimate is None else f"{estimate:.2f}"))
    try:
        run_id = collect.start_run(conn, a.investigation, provider, limits)
    except collect.CollectionError as e:
        raise SystemExit(str(e))
    print(f"run {run_id}")
    _execute(conn, provider, run_id)


def cmd_resume(a):
    conn = connect(a.db)
    _execute(conn, _provider(a.provider), a.run)


def _execute(conn, provider, run_id):
    try:
        status = collect.Collector(conn, provider).execute(run_id)
    except KeyboardInterrupt:
        raise SystemExit(f"interrupted; resume with: resume --run {run_id}")
    except ProviderError as e:
        raise SystemExit(f"provider error: {e.code}: {e}")
    print(f"run {run_id}: {status}")
    _print_summary(report.run_summary(conn, run_id))
    prev = conn.execute(
        "SELECT MAX(id) FROM run WHERE investigation_id=(SELECT investigation_id FROM run WHERE id=?) AND id<?"
        " AND status IN ('completed','partial')",
        (run_id, run_id),
    ).fetchone()[0]
    if prev:
        for m in report.record_notifications(conn, prev, run_id):
            print("notice: " + _text(m))


def cmd_cancel(a):
    conn = connect(a.db)
    collect.request_cancel(conn, a.run, interrupted=a.interrupted)
    print(conn.execute("SELECT status FROM run WHERE id=?", (a.run,)).fetchone()[0])


def cmd_summary(a):
    conn = connect(a.db)
    s = report.run_summary(conn, a.run)
    if a.json:
        _print_json(s)
    else:
        _print_summary(s)


def _print_summary(s):
    an = s["analysis"] or {}
    seed = s["seed"]
    print(f"Seed: {_text(seed['name'])} [{seed['external_id']}]   run {s['run_id']} ({s['status']}"
          + (f": {s['stop_reason']}" if s["stop_reason"] else "") + ")")
    print(f"Sample: {s['sampling_label']}")
    if not an:
        print("No analysis yet.")
        return
    cov = an["coverage"]
    print(f"Seed reviews fetched: {an['seed_reviews_fetched']}  (without reviewer ID: {an['seed_reviews_without_reviewer_id']})")
    print(f"Reviewers selected: {an['selected_reviewers']}   histories retrieved: {an['retrieved_histories']}"
          f"   retrieval coverage: {an['retrieval_coverage']:.0%}")
    print("Histories: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in cov.items()))
    print(f"Observed destinations: {an['observed_destinations']}   requests: {s['request_count']}   cost: {s['cost']:.2f}")
    print(f"Finding: {an['finding']}")
    print("Limitations:")
    for line in an["limitations"]:
        print(f"  - {line}")


def _filters(a) -> report.Filters:
    return report.Filters(
        min_shared=a.min_shared, max_distance_km=a.max_distance_km, min_rating=a.min_rating,
        max_rating=a.max_rating, since=a.since, until=a.until, category=a.category,
    )


def cmd_connections(a):
    conn = connect(a.db)
    rows = report.connections(conn, a.run, _filters(a))
    if a.json:
        return _print_json(rows)
    an = report.run_summary(conn, a.run)["analysis"] or {}
    if not rows:
        print(an.get("finding", "no analysis"))
        return
    print(f"{'conn':>5}  {'shared':>6}  {'of retrieved':>14}  {'of selected':>13}  {'km':>7}  {'7d':>5}  {'text':>4}  destination")
    for r in rows:
        sig = r["signals"]
        t = sig["timing"]
        dist = "" if sig["distance_km"] is None else f"{sig['distance_km']:.1f}"
        shared = str(r["shared_reviewers"])
        if r["shared_reviewers_matching_filters"] is not None:
            shared = f"{r['shared_reviewers_matching_filters']}/{shared}"
        d = r["destination"]
        print(
            f"{r['connection_id']:>5}  {shared:>6}  "
            f"{_pct(r['share_of_retrieved'])} of {r['retrieved_denominator']:<4}  "
            f"{_pct(r['share_of_selected'])} of {r['selected_denominator']:<3}  {dist:>7}  "
            f"{t['within_window']}/{t['evaluable']:<3}  {sig['similar_text']['accounts_flagged']:>4}  "
            f"{_text(d['name'], 40)} · {_text(d['category'])} · {_text(d['address'], 40)}"
        )
    print(f"\nRetrieval coverage {an.get('retrieval_coverage', 0):.0%}. Overlap is an observation, not a finding of fraud.")


def _pct(x):
    return "   –" if x is None else f"{x:>4.0%}"


def cmd_evidence(a):
    conn = connect(a.db)
    rows = report.evidence(conn, a.connection)
    if a.json:
        return _print_json(rows)
    for ev in rows:
        print(f"— account {ev['reviewer_external_id']} (label: {_text(ev['display_name'])}) {ev['profile_url'] or ''}")
        for side, label in (("seed", "seed"), ("dest", "destination")):
            print(f"    {label}: {ev[f'{side}_rating']}★  {ev[f'{side}_published_raw'] or '?'} "
                  f"[{ev[f'{side}_precision']}]  {ev[f'{side}_source_url'] or ''}  fetched {ev[f'{side}_fetched_at']}")
            if ev[f"{side}_text"]:
                print(f"      “{_text(ev[f'{side}_text'], 300)}”")
        flags = []
        if ev["within_window"]:
            flags.append("within timing window")
        elif ev["within_window"] is None:
            flags.append("timing unavailable (imprecise date)")
        if ev["similar_text"]:
            flags.append("near-duplicate text")
        if flags:
            print("    " + "; ".join(flags))
    print(f"\n{len(rows)} distinct accounts")


def cmd_export_csv(a):
    conn = connect(a.db)
    data = report.evidence_csv(conn, a.run, _filters(a))
    if a.out:
        Path(a.out).write_text(data, encoding="utf-8")
    else:
        sys.stdout.write(data)


def cmd_export_json(a):
    conn = connect(a.db)
    data = report.run_json(conn, a.run)
    if a.out:
        Path(a.out).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    else:
        _print_json(data)


def cmd_compare(a):
    _print_json(report.compare_runs(connect(a.db), a.earlier, a.later))


def cmd_reanalyze(a):
    conn = connect(a.db)
    params = {"window_days": a.window_days, "text_similarity": a.text_similarity, "text_min_chars": a.text_min_chars}
    analyze_run(conn, a.run, params)
    _print_summary(report.run_summary(conn, a.run))


def cmd_schedule(a):
    conn = connect(a.db)
    maintenance.set_refresh_schedule(conn, a.investigation, None if a.off else "weekly")
    print("weekly refresh " + ("disabled" if a.off else "enabled"))


def cmd_due(a):
    _print_json(maintenance.due_refreshes(connect(a.db)))


def cmd_purge(a):
    _print_json(maintenance.purge(connect(a.db), a.raw_days, a.observation_days))


def cmd_delete(a):
    _print_json(maintenance.delete_investigation(connect(a.db), a.investigation))


def cmd_import_csv(a):
    data = dataset_from_csv(a.seed_business, a.seed_reviews, a.histories, a.businesses, a.profiles)
    Path(a.out).write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"wrote {a.out}: {len(data['profiles'])} profiles")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="reviewcrossover", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help, db=True, provider=False):
        p = sub.add_parser(name, help=help)
        if db:
            p.add_argument("--db", required=True)
        if provider:
            p.add_argument("--provider", required=True, help="import:<dataset.json>")
        p.set_defaults(fn=fn)
        return p

    def filters(p, min_shared):
        p.add_argument("--min-shared", type=int, default=min_shared)
        p.add_argument("--max-distance-km", type=float)
        p.add_argument("--min-rating", type=int)
        p.add_argument("--max-rating", type=int)
        p.add_argument("--since", help="destination review date, YYYY-MM-DD")
        p.add_argument("--until")
        p.add_argument("--category")

    p = add("resolve", cmd_resolve, "show the business a URL or ID resolves to", db=False, provider=True)
    p.add_argument("business")
    p = add("new", cmd_new, "create an investigation after confirming the resolved business", provider=True)
    p.add_argument("business")
    p.add_argument("--owner", default="local")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p = add("run", cmd_run, "start a collection run", provider=True)
    p.add_argument("--investigation", type=int, required=True)
    p.add_argument("--sample", type=int, default=100)
    p.add_argument("--per-profile", type=int, default=100)
    p.add_argument("--max-seed-reviews", type=int, default=1000)
    p.add_argument("--max-requests", type=int)
    p.add_argument("--max-cost", type=float)
    p.add_argument("--cache-days", type=float, default=7)
    p = add("resume", cmd_resume, "resume an interrupted run", provider=True)
    p.add_argument("--run", type=int, required=True)
    p = add("cancel", cmd_cancel, "cancel a run")
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--interrupted", action="store_true", help="the run's process is gone: close it now")
    p = add("summary", cmd_summary, "run summary with coverage and limitations")
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--json", action="store_true")
    p = add("connections", cmd_connections, "ranked destinations sharing reviewers with the seed")
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--json", action="store_true")
    filters(p, 3)
    p = add("evidence", cmd_evidence, "the accounts and reviews behind one connection")
    p.add_argument("--connection", type=int, required=True)
    p.add_argument("--json", action="store_true")
    p = add("export-csv", cmd_export_csv, "evidence CSV")
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--out")
    filters(p, 1)
    p = add("export-json", cmd_export_json, "full run JSON")
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--out")
    p = add("compare", cmd_compare, "observation changes between two runs")
    p.add_argument("--earlier", type=int, required=True)
    p.add_argument("--later", type=int, required=True)
    p = add("reanalyze", cmd_reanalyze, "recompute a run's connections with other signal parameters")
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--window-days", type=int, default=7)
    p.add_argument("--text-similarity", type=float, default=0.85)
    p.add_argument("--text-min-chars", type=int, default=80)
    p = add("schedule", cmd_schedule, "enable or disable weekly refresh for an investigation")
    p.add_argument("--investigation", type=int, required=True)
    p.add_argument("--off", action="store_true")
    add("due", cmd_due, "investigations due a scheduled refresh")
    p = add("purge", cmd_purge, "apply retention")
    p.add_argument("--raw-days", type=float, default=maintenance.RAW_PAYLOAD_DAYS)
    p.add_argument("--observation-days", type=float, default=maintenance.OBSERVATION_DAYS)
    p = add("delete-investigation", cmd_delete, "delete an investigation and data nothing else needs")
    p.add_argument("--investigation", type=int, required=True)
    p = add("import-csv", cmd_import_csv, "build an import dataset from CSV files", db=False)
    p.add_argument("--seed-business", required=True)
    p.add_argument("--seed-reviews", required=True)
    p.add_argument("--histories", required=True)
    p.add_argument("--businesses")
    p.add_argument("--profiles")
    p.add_argument("--out", required=True)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
