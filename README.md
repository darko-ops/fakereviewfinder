# fakereviewfinder — Review Crossover

An investigation tool that starts with one business, follows its reviewers'
public review histories, and lists the other businesses those same accounts
reviewed: ranked, with denominators, coverage, and the evidence behind every
count. The spec is [`docs/spec.md`](docs/spec.md).

It detects **overlap**, not fraud. A shared reviewer is an observation; nothing
here says an account is fake, that a business bought reviews, or who ran a
campaign.

## Status

This is Milestone 2 of the spec (analysis engine, CLI, SQLite), plus the
provider contract and import fallback from Milestone 1.

- **No collection vendor is connected.** Milestone 1 (the provider pilot) is
  not code: it means validating a vendor's IDs, coverage, cost and terms
  before anything collects automatically. Until then, data comes in through
  `ImportProvider`, from JSON or CSV, using the same contract a vendor adapter
  will implement (`reviewcrossover/provider.py`).
- The web interface, Postgres and background worker (Milestone 3) are not built.
- Refresh, snapshots, cache reuse, cancellation and observation-change notices
  (Milestone 4) exist at the CLI level. Weekly refresh is off by default;
  `due` lists investigations that are due, and nothing schedules itself.

Standard library only; Python 3.11 or newer.

## Try it

```sh
python3 -m reviewcrossover.fixture > fixture.json      # synthetic, all invented
python3 -m reviewcrossover new --db rc.db --provider import:fixture.json https://maps.example/seed-1
python3 -m reviewcrossover run --db rc.db --provider import:fixture.json --investigation 1
python3 -m reviewcrossover connections --db rc.db --run 1
python3 -m reviewcrossover evidence --db rc.db --connection <id>
python3 -m reviewcrossover export-csv --db rc.db --run 1 --out evidence.csv
python3 -m reviewcrossover export-json --db rc.db --run 1 --out run.json
```

From your own CSVs (see `examples/` for the columns):

```sh
python3 -m reviewcrossover import-csv --seed-business seed-x \
  --seed-reviews examples/seed_reviews.csv --histories examples/histories.csv \
  --businesses examples/businesses.csv --profiles examples/profiles.csv --out data.json
```

Other commands: `resolve`, `resume`, `cancel [--interrupted]`, `summary`,
`compare`, `reanalyze`, `schedule [--off]`, `due`, `purge`,
`delete-investigation`. Each has `--help`.

Tests (the spec's acceptance criteria, against `reviewcrossover/fixture.py`):

```sh
python3 -m unittest discover -s tests
```

## How it maps to the spec

| Spec | Where |
|---|---|
| Provider contract: records, cursor, fetched_at, provider, completion status | `models.Page`, `provider.Provider` |
| Timestamp precision; "2 months ago" never becomes a date | `models.parse_timestamp` |
| Platform-scoped IDs, unique constraints, observations kept per run | `db.py`, `store.py` |
| Sampling, request/spend caps, retries, cursors, resume, cancel, cache | `collect.py` |
| S, E, O(B), both denominators, coverage, supporting signals | `analyze.py` |
| Filters, evidence drawer, CSV/JSON export, snapshot comparison, notices | `report.py` |
| Retention, deletion, refresh schedule | `maintenance.py` |

## Decisions the spec left open

These are choices made here; change them if they're wrong.

- **What counts toward E (retrieved).** A history counts when at least one
  record came back and its status is complete, capped or unknown. Empty,
  unavailable and failed histories don't count. A fetch that fails partway
  through pagination is `failed`, and its records are not used.
- **Statuses.** `complete`, `capped`, `unknown` (records returned,
  completeness not stated), `empty`, `unavailable`, `failed`, and
  `unknown_no_records` (the provider said nothing about the profile) are
  reported separately, as is `not_attempted` (budget, cancel or interruption).
- **Reviews without a provider ID.** The key is the reviewer/business pair
  only when the provider declares `one_review_per_pair`. Otherwise it is a
  content hash, which can turn an edit into a second review. Overlap counts
  distinct accounts either way, so it never inflates `shared_reviewers`.
- **One evidence row per account.** When an account has several reviews of a
  destination, evidence shows its most recent precisely dated one.
- **Timing window.** Only exact- and day-precision dates are compared; the
  difference is in whole days. Month and relative dates count as "unavailable".
- **Text similarity.** Jaccard similarity of character trigrams on
  NFKC-casefolded text with punctuation removed. A review needs at least 80
  normalised characters and a similarity of at least 0.85. Seed-to-destination
  and account-to-account pairs at the same destination are counted
  separately. `reanalyze` reruns the analysis with other thresholds.
- **Repeated destinations.** For every pair of destinations, take the seed
  accounts observed at both. Pairs that share the same account set (three or
  more accounts) merge into one group.
- **"Insufficient observed data".** Shown when nothing reaches the display
  threshold and retrieval coverage is below 50% (or zero).
- **Cache reuse.** A cached fetch is reused when it is from the same provider,
  less than `--cache-days` old (default 7; 0 disables reuse), and was made under
  the same per-profile cap or ended within this one. Failed fetches are never
  reused.
- **Chain context.** Shown as the number of other observed destinations with
  the same normalised name. It is context only; names are never merge keys.

Review text is untrusted. The CLI strips control characters, and CSV export
prefixes formula-like cells with `'`.
