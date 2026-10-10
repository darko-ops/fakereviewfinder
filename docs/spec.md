# Review Crossover — MVP Product and Technical Specification

Version 0.1 · October 9, 2026 · Working title

## 1. Product definition

An investigation tool that starts with a business, follows its reviewers' publicly visible review histories, and identifies other businesses reviewed by the same accounts. It helps an analyst inspect possible coordination using traceable evidence.

**Core question:** Which businesses share reviewers, and which connections deserve a closer look?

The product detects overlap and patterns. It does not establish that an account is fake, that a business purchased reviews, or who operated a campaign. Coronet Seafood inspired the use case; this specification makes no allegation about it.

## 2. Users and value

Initial user: a solo researcher investigating one business at a time. Later users could include consumer investigators, journalists, and reputation or trust-and-safety teams.

The value is reducing hours of opening reviewer profiles to a ranked, inspectable list of connections. The differentiator is cross-business account overlap, rather than judging individual reviews by whether their prose sounds artificial.

## 3. MVP scope

### Included

- Google Maps business URL or place ID as the starting point.
- Automated collection through a replaceable provider adapter; CSV/JSON import as a fallback.
- Up to 100 distinct seed reviewers by default, with a visible sampling policy.
- Public reviewer-history collection, capped initially at 100 reviews per profile.
- Stable account and business identifiers, deduplication, and source links.
- Ranked destination businesses, shared-reviewer evidence, and coverage reporting.
- Filters for rating, available date ranges, geography, and minimum shared reviewers.
- Manual refresh and optional weekly monitoring per investigation.
- CSV evidence export and JSON run export.

### Excluded

- Automatic fraud verdicts, accusations, takedown requests, or public blacklists.
- Real-world identification of reviewers, contact enrichment, or facial matching.
- Unbounded recursive crawling of discovered businesses.
- A calibrated probability of fraud or an AI-generated authenticity verdict.
- Multiple platforms, billing, team collaboration, and a dedicated graph database.

## 4. Primary workflow

1. User pastes a business URL; the app resolves and displays the business name, address, and stable ID for confirmation before starting collection.
2. User selects run limits and sees an estimated collection cost if the provider supports one. Otherwise show "estimate unavailable" and require a configured request/spend cap.
3. Collector imports the seed reviews and chooses distinct reviewers using the configured sample.
4. Collector requests each selected reviewer's public history once, reusing sufficiently fresh cached data.
5. Normalizer links observations by stable IDs and records unavailable or truncated histories explicitly.
6. Analyzer groups destinations by shared seed reviewers and attaches timing, text, distance, and category signals where available.
7. User opens a destination to inspect the accounts and source reviews that created the connection.
8. A refresh creates a new snapshot and shows added observations without rewriting historical conclusions.

Only one outward expansion is automatic: seed business → seed reviewers → other businesses. Investigating a discovered business is a separate bounded run.

## 5. Data-access feasibility gate

Data collection is the first milestone, before the dashboard. A business-review scraper and a reviewer-history collector are separate capabilities; a vendor supporting one may not support the other.

Google's documented place objects expose up to five reviews, so the official Places interface is insufficient for the intended sample by itself. Third-party tools advertise richer business review exports and contributor histories, but their availability, permitted uses, identifiers, coverage, and reliability must be validated before selection. No vendor is selected by this spec.

Run a bounded pilot against one seed and 20 reviewer profiles:

- Confirm stable reviewer IDs, stable business IDs, source URLs, pagination, timestamp precision, and failure statuses.
- Manually compare five profiles with the returned records; document omissions and mismatches.
- Measure cost, latency, successful histories, and cap/truncation behavior.
- Verify the provider's terms support the intended collection, retention, analysis, and exports.
- Distinguish private/unavailable profiles from transient provider failures.

Proceed to automated MVP only if identity joins are reliable, coverage limitations are observable, and the provider supports the intended use. Otherwise deliver an import-based prototype and keep automated collection blocked. Missing stable IDs exclude an observation from identity-based matching; never substitute a display-name match.

## 6. Proposed architecture

Keep the first build small: Python collector/analyzer with SQLite and a CLI. After the pilot works, add a Next.js/TypeScript interface, PostgreSQL, and a background worker using a database-backed job queue. These are proposed implementation choices, not claims about required library versions.

| Component | Responsibility |
|---|---|
| Provider adapter | Resolve business, fetch seed reviews, fetch contributor histories, expose cursors and limits |
| Worker | Bounded collection, retries, normalization, and analysis |
| Database | Canonical identities, observations, jobs, runs, and evidence relationships |
| Analysis module | Deterministic overlap counts and transparent supporting signals |
| Web app | Investigation setup, progress, ranked table, evidence drawer, exports |
| Scheduler | Enqueue refreshes; never start a second active refresh for the same investigation |

Provider contract: each response contains records, next_cursor, fetched_at, source/provider, and completion status. Completion status must distinguish end reached, capped, unavailable, failed, and unknown. Store timestamp precision as exact, day, month, relative, or unknown. "Two months ago" must never become an invented exact posting date.

## 7. Data model

All internal keys are separate from external IDs. Scope external IDs by platform and identifier type. Keep mappings between place IDs, other provider business IDs, and URLs only when verified; names and addresses alone are not automatic merge keys.

| Entity | Minimum fields |
|---|---|
| Business | id, platform, external_id, id_type, name, address, latitude/longitude when available, category, source_url, observed_at |
| Reviewer | id, platform, external_id, profile_url, display_name, first_seen_at, last_seen_at |
| Review | id, external_review_id when available, reviewer_id, business_id, rating, text, published_at, timestamp_precision, source_url |
| Review observation | review_id, run_id, fetched_at, provider, payload_hash, current content/version |
| Investigation | id, seed_business_id, owner_id, settings, refresh_schedule, created_at |
| Run | id, investigation_id, sampling_policy, limits, status, started_at, finished_at, cost, analysis_version |
| Profile fetch | run_id, reviewer_id, status, fetched_count, reported_total if available, cursor, capped, error_code |
| Connection | run_id, seed_id, destination_id, shared_count, denominator, supporting_signal_values |
| Connection evidence | connection_id, reviewer_id, seed_review_id, destination_review_id |

Unique constraints on platform-scoped entity IDs and provider review IDs prevent duplicates. If review IDs are absent, use a documented reviewer/business composite only when provider semantics establish one current review per pair; preserve edits as observations. Never create multiple shared reviewers because one account has multiple rows.

## 8. Overlap analysis

### Seed-relative discovery

Let S be the distinct selected seed reviewers. Let E be the subset for which some usable public history was retrieved; histories can still be capped. For destination B, let O(B) be the distinct accounts in E with an observed review of B.

- Shared reviewers = |O(B)|.
- Observed share among retrieved profiles = |O(B)| / |E|, when |E| > 0.
- Observed share of selected sample = |O(B)| / |S|.
- Retrieval coverage = |E| / |S|; show full, partial, and unavailable profile counts separately.

These are observed sample metrics. They are not the proportion of all customers who visited both businesses. Example: 12 matches among 80 retrieved profiles from 100 selected reviewers means 15% of retrieved profiles and 12% of the selected sample, with 80% retrieval coverage.

The default table sorts by shared reviewers descending, then destination name for stable ties. Start with a minimum of three shared reviewers for the main table, with an option to view smaller overlaps. The threshold is a navigation default, not evidence of fraud.

Do not compute a global Jaccard similarity from a seed-expanded crawl: destination reviewer populations are incompletely sampled. If both businesses later receive independent comparable collections, an explicitly labeled observed-set similarity can be added.

### Supporting signals

| Signal | MVP behavior | Limitation |
|---|---|---|
| Geography | Show seed-to-destination distance, if coordinates exist; permit distance filters | Travel and tourism can explain distant reviews |
| Category | Show categories and a cross-category flag | People legitimately use unrelated business types |
| Timing | Show count of shared accounts reviewing both within a configurable seven-day interval, using sufficiently precise dates | Publication date is not necessarily visit date |
| Similar text | Flag long near-duplicate text using a deterministic similarity method; show both originals | Templates and common wording can be innocent |
| Repeated destinations | Show groups of at least three seed accounts sharing at least two non-seed destinations | Counts remain conditional on the seed sample |
| Rating pattern | Show rating distribution across evidence reviews, including negative reviews | Coordination can involve negative attacks as well as praise |

For text matching, start with normalized character trigrams, at least 80 characters, and similarity ≥0.85. Treat these as provisional engineering defaults to tune against examples. Exclude rating-only reviews from text analysis. Avoid a combined 0–100 "fake score" in v1; counts, explicit signals, and evidence are more defensible.

Popularity and local activity are important confounders. Show known chain/category/local context, but do not claim to mathematically adjust for population popularity using only this biased seed sample. A future significance model requires a broader, independently collected comparison corpus.

## 9. Interface

New investigation: URL, resolved business, review sample size, collection limits, cost cap, and start action. Default sample is the newest 100 distinct reviewers; label it as recency-biased. Preserve source order and selection settings.

Run summary: seed reviews fetched, reviewers selected, histories retrieved, histories capped/unavailable/failed, observed destinations, collection date, and sample limitations. Zero matches with poor coverage must display "insufficient observed data," not a clean bill of health.

Connections table: destination, location, category, shared accounts, observed share with denominator, distance, timing/text indicators, and evidence action.

Evidence drawer: one row per shared account, containing its seed review and destination review, ratings, dates with precision, source links, and collection timestamps. Display names are labels; IDs determine matches.

Refresh history: compare snapshots by new observations and changed coverage. A review absent from a later partial fetch is not automatically "deleted."

Optional later view: an interactive reviewer/business network. The table is the default because it supports inspection better than a large dense graph.

## 10. Automation and operating limits

- Initial defaults: 100 seed reviewers, 100 history records per profile, and no recursive expansion. Up to roughly 10,000 history rows per run; actual provider request and billing counts may differ.
- Fetch only the seed and uncached/stale profiles; cache freshness defaults to seven days and remains visible.
- Configure concurrency to provider limits. Retry transient failures at most three times with backoff; stop on authorization failures, explicit access restrictions, or budget exhaustion.
- Persist cursors and job progress so interrupted runs can resume idempotently.
- Mark outcomes completed, partial, failed, or canceled. Partial data remains inspectable with coverage labels.
- Optional weekly refresh is disabled until the user enables it for an investigation. This spec creates no live schedule.
- Monitoring adds an in-app notification when at least three newly observed distinct accounts create a new connection, or when an existing connection gains at least three accounts. Label these as observation changes, not proven new review activity.
- Keep provider credentials server-side. Restrict access to the investigation owner; exports remain private by default.

## 11. Evidence and retention

Every displayed connection must resolve to its underlying observations and collection run. Record the analysis version and collection settings so exports can be reproduced. Preserve original review text separately from normalized text. Treat review content as untrusted input: escape it in the interface, and neutralize spreadsheet formula prefixes in CSV exports.

Store only public review information needed for analysis; omit profile photos and contact details. Proposed default retention is 90 days for raw provider payloads and one year for normalized observations, subject to validated provider terms. Support deleting an investigation and its unneeded data. Do not infer account authenticity from names, photos, or demographic characteristics.

## 12. Acceptance criteria

1. Two accounts with identical display names remain separate; a renamed account with the same stable ID remains one account.
2. A deterministic fixture with 100 selected reviewers, 80 retrieved histories, and 12 destination matches returns 12, 15%, 12%, and 80% with correct labels.
3. Repeated pagination rows and edited reviews do not inflate reviewer overlap.
4. Private, capped, failed, empty, and unknown history states remain distinct in both UI and export.
5. Imprecise timestamps are excluded from exact-window calculations and reported as unavailable for that signal.
6. Every shared-reviewer count can be expanded into exactly that number of distinct evidence accounts.
7. A common local destination with many shared reviewers is shown as overlap without an automatic fraud label.
8. Resume after interruption produces the same normalized result as an uninterrupted run, given identical source responses.
9. Budget caps stop additional requests and mark the run partial; cancellation stops further job expansion.
10. Refetching partial data never falsely declares earlier reviews deleted.
11. Exports reproduce run counts and preserve source links, timestamps, denominators, and limitations.

## 13. Build sequence and decision points

**Milestone 1 — Collection pilot:** validate one provider or establish import fallback. Deliver sample records, coverage and cost measurements, and a go/no-go decision.

**Milestone 2 — Analysis engine:** implement normalization, identity joins, overlap counts, evidence exports, and fixture-based acceptance tests. Deliver CLI plus SQLite database.

**Milestone 3 — Investigator interface:** add setup, background job status, connections table, coverage summary, and evidence drawer. Deliver a private single-user MVP.

**Milestone 4 — Monitoring:** add cache reuse, bounded refresh, snapshots, cancellation, and observation-change notifications.

**Later:** comparable independent business samples, broader baselines, connected-component exploration, calibrated statistical models, and additional platforms. Do not prioritize AI summaries until the evidence pipeline is reliable.

Success measures: time to inspect the first useful connection, usable profile coverage, cost per retrieved history, deduplication accuracy, and analyst-confirmed usefulness of the top connections. Do not report fraud-detection accuracy without independently labeled evaluation data.

## 14. Sources and unresolved assumptions

Sources checked during the concept discussion on October 9, 2026. Provider descriptions establish advertised features only; no collection tool has been tested for this project.

- Google Maps JavaScript API policies: documented limit of up to five reviews per Place object. https://developers.google.com/maps/documentation/javascript/policies
- Google place review documentation: author attribution and profile links. https://developers.google.com/maps/documentation/javascript/place-reviews
- Apify business-review collector input documentation, candidate only. https://apify.com/compass/google-maps-reviews-scraper/input-schema
- Contributor-history collector repository, candidate only. https://github.com/lexis-solutions-data-services/google-maps-contributor-reviews-scraper
- Google Maps content policy: fake engagement and rating manipulation. https://support.google.com/contributionpolicy/answer/7400114

Open decisions: collection provider, permissible retention/export rights, acceptable per-run cost, actual pagination completeness, and timestamp precision. These are resolved in Milestone 1 rather than assumed in the product.
