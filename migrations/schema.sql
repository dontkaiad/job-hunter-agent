-- job-hunter-agent SQLite schema (DESIGN.md §2)
-- Datetimes are ISO-8601 UTC strings. Booleans stored as INTEGER (0/1).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS work_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    state             TEXT    NOT NULL,
    source_channel    TEXT,
    source_link       TEXT,
    source_message_id TEXT,
    raw_text          TEXT,
    extracted_json    TEXT,
    relevance_score   REAL,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    CHECK (state IN (
        'discovered','extracted','scored','rejected','surfaced',
        'skipped','backlog','approved','researched','drafted','sent','closed'
    ))
);

CREATE INDEX IF NOT EXISTS idx_work_items_state      ON work_items (state);
CREATE INDEX IF NOT EXISTS idx_work_items_updated_at ON work_items (updated_at);

-- Dedup on (source_channel, source_message_id), enforced only when
-- source_message_id is present (NULLs are distinct in SQLite UNIQUE indexes,
-- and the partial WHERE keeps NULL message ids out of the constraint).
CREATE UNIQUE INDEX IF NOT EXISTS uniq_work_items_source
    ON work_items (source_channel, source_message_id)
    WHERE source_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS state_transitions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL REFERENCES work_items(id),
    from_state TEXT,
    to_state   TEXT    NOT NULL,
    kind       TEXT,
    actor      TEXT,
    reason     TEXT,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transitions_item       ON state_transitions (item_id);
CREATE INDEX IF NOT EXISTS idx_transitions_created_at ON state_transitions (created_at);

-- Per-channel ingestion watermark: the datetime of the newest post processed
-- for a channel. Drives INCREMENTAL ingestion (only fetch posts newer than the
-- watermark; a channel with no row is NEW -> use the lookback window).
-- last_post_at is a UTC ISO-8601 string (offset-aware), matching the rest of
-- the schema's datetime convention.
CREATE TABLE IF NOT EXISTS channel_state (
    channel      TEXT PRIMARY KEY,
    last_post_at TEXT,
    updated_at   TEXT NOT NULL
);
