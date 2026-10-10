"""SQLite storage: canonical identities, observations, runs and evidence.

Internal keys are separate from external IDs, and external IDs are scoped by
platform and identifier type. The unique constraints are what keep a repeated
pagination row, an edited review or a renamed account from becoming a second
entity (acceptance criteria 1 and 3).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS business (
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL,
  name TEXT,
  address TEXT,
  latitude REAL,
  longitude REAL,
  category TEXT,
  source_url TEXT,
  observed_at TEXT NOT NULL
);

-- Every external identifier a business is known by. The first is the one it
-- was created from; others are added only when a provider verifies them.
CREATE TABLE IF NOT EXISTS business_external_id (
  platform TEXT NOT NULL,
  id_type TEXT NOT NULL,
  external_id TEXT NOT NULL,
  business_id INTEGER NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  verified_by TEXT,
  PRIMARY KEY (platform, id_type, external_id)
);

CREATE TABLE IF NOT EXISTS reviewer (
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL,
  external_id TEXT NOT NULL,
  profile_url TEXT,
  display_name TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  UNIQUE (platform, external_id)
);

-- review_key is the provider review ID when there is one; otherwise a
-- reviewer/business pair key when the provider establishes one current review
-- per pair; otherwise a content key (documented as weaker in the README).
CREATE TABLE IF NOT EXISTS review (
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL,
  review_key TEXT NOT NULL,
  external_review_id TEXT,
  reviewer_id INTEGER NOT NULL REFERENCES reviewer(id),
  business_id INTEGER NOT NULL REFERENCES business(id),
  rating INTEGER,
  text TEXT,
  published_raw TEXT,
  published_at TEXT,
  timestamp_precision TEXT NOT NULL,
  source_url TEXT,
  UNIQUE (platform, review_key)
);
CREATE INDEX IF NOT EXISTS review_reviewer ON review(reviewer_id);

CREATE TABLE IF NOT EXISTS investigation (
  id INTEGER PRIMARY KEY,
  seed_business_id INTEGER NOT NULL REFERENCES business(id),
  owner_id TEXT NOT NULL,
  settings TEXT NOT NULL,
  refresh_schedule TEXT,          -- NULL (disabled) or 'weekly'
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run (
  id INTEGER PRIMARY KEY,
  investigation_id INTEGER NOT NULL REFERENCES investigation(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  sampling_policy TEXT NOT NULL,
  limits TEXT NOT NULL,
  status TEXT NOT NULL,           -- pending, running, completed, partial, failed, canceled
  stop_reason TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  finished_at TEXT,
  cost REAL NOT NULL DEFAULT 0,
  request_count INTEGER NOT NULL DEFAULT 0,
  cost_estimate REAL,
  seed_cursor TEXT,
  seed_done INTEGER NOT NULL DEFAULT 0,
  seed_status TEXT,
  seed_reviews_fetched INTEGER NOT NULL DEFAULT 0,
  seed_reviews_without_reviewer_id INTEGER NOT NULL DEFAULT 0,
  records_without_business_id INTEGER NOT NULL DEFAULT 0,
  analysis_version TEXT,
  analysis TEXT
);

CREATE TABLE IF NOT EXISTS review_observation (
  id INTEGER PRIMARY KEY,
  review_id INTEGER NOT NULL REFERENCES review(id) ON DELETE CASCADE,
  run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  fetched_at TEXT NOT NULL,
  provider TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  rating INTEGER,
  text TEXT,
  published_raw TEXT,
  published_at TEXT,
  timestamp_precision TEXT NOT NULL,
  source_url TEXT,
  UNIQUE (review_id, run_id, payload_hash)
);

CREATE TABLE IF NOT EXISTS raw_payload (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  request TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  payload TEXT NOT NULL
);

-- S: the selected seed reviewers, in source order.
CREATE TABLE IF NOT EXISTS run_seed_reviewer (
  run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  reviewer_id INTEGER NOT NULL REFERENCES reviewer(id),
  position INTEGER NOT NULL,
  seed_review_id INTEGER NOT NULL REFERENCES review(id),
  seed_observation_id INTEGER NOT NULL REFERENCES review_observation(id),
  PRIMARY KEY (run_id, reviewer_id),
  UNIQUE (run_id, position)
);

CREATE TABLE IF NOT EXISTS profile_fetch (
  run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  reviewer_id INTEGER NOT NULL REFERENCES reviewer(id),
  status TEXT NOT NULL,
  fetched_count INTEGER NOT NULL DEFAULT 0,
  reported_total INTEGER,
  cursor TEXT,
  capped INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  fetched_at TEXT,
  source_run_id INTEGER REFERENCES run(id),  -- set when reused from cache
  PRIMARY KEY (run_id, reviewer_id)
);

-- The distinct reviews observed in one profile fetch, and the observation
-- (content version) this run saw last.
CREATE TABLE IF NOT EXISTS profile_fetch_item (
  run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  reviewer_id INTEGER NOT NULL REFERENCES reviewer(id),
  review_id INTEGER NOT NULL REFERENCES review(id),
  observation_id INTEGER NOT NULL REFERENCES review_observation(id),
  PRIMARY KEY (run_id, reviewer_id, review_id)
);

CREATE TABLE IF NOT EXISTS connection (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  seed_id INTEGER NOT NULL REFERENCES business(id),
  destination_id INTEGER NOT NULL REFERENCES business(id),
  shared_count INTEGER NOT NULL,
  retrieved_denominator INTEGER NOT NULL,
  selected_denominator INTEGER NOT NULL,
  signals TEXT NOT NULL,
  analysis_version TEXT NOT NULL,
  UNIQUE (run_id, destination_id)
);

CREATE TABLE IF NOT EXISTS connection_evidence (
  connection_id INTEGER NOT NULL REFERENCES connection(id) ON DELETE CASCADE,
  reviewer_id INTEGER NOT NULL REFERENCES reviewer(id),
  seed_review_id INTEGER NOT NULL REFERENCES review(id),
  seed_observation_id INTEGER NOT NULL REFERENCES review_observation(id),
  destination_review_id INTEGER NOT NULL REFERENCES review(id),
  destination_observation_id INTEGER NOT NULL REFERENCES review_observation(id),
  similar_text INTEGER NOT NULL DEFAULT 0,
  within_window INTEGER,          -- NULL when either date is too imprecise
  PRIMARY KEY (connection_id, reviewer_id)
);

CREATE TABLE IF NOT EXISTS notification (
  id INTEGER PRIMARY KEY,
  investigation_id INTEGER NOT NULL REFERENCES investigation(id) ON DELETE CASCADE,
  run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL,
  read INTEGER NOT NULL DEFAULT 0
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


class transaction:
    """BEGIN IMMEDIATE ... COMMIT, rolled back on any exception.

    Each provider page is stored in one of these together with its cursor, so
    an interruption leaves either the whole page or none of it.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
