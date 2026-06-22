-- job-hunter-agent PostgreSQL schema (DESIGN.md §2)
--
-- Behaviour-preserving port of migrations/schema.sql from SQLite. The datetime
-- convention is DELIBERATELY UNCHANGED: created_at / updated_at / last_post_at
-- stay TEXT columns holding UTC ISO-8601 strings produced by clock.now_iso().
-- They are NOT timestamptz — switching types would change the now_iso() string
-- round-trip and the lexicographic watermark comparison that the incremental
-- ingestion relies on (all strings are same-format UTC ISO with +00:00).
--
-- init_db() runs these statements one-by-one (psycopg3 has no executescript).
-- Every statement is idempotent (IF NOT EXISTS), so init_db is safe to re-run.

CREATE TABLE IF NOT EXISTS work_items (
    id                SERIAL PRIMARY KEY,
    state             TEXT             NOT NULL,
    source_channel    TEXT,
    source_link       TEXT,
    source_message_id TEXT,
    raw_text          TEXT,
    extracted_json    TEXT,
    relevance_score   DOUBLE PRECISION,
    created_at        TEXT             NOT NULL,
    updated_at        TEXT             NOT NULL,
    CHECK (state IN (
        'discovered','extracted','scored','rejected','surfaced',
        'skipped','backlog','approved','researched','drafted','sent','closed'
    ))
);

CREATE INDEX IF NOT EXISTS idx_work_items_state      ON work_items (state);
CREATE INDEX IF NOT EXISTS idx_work_items_updated_at ON work_items (updated_at);

-- Dedup on (source_channel, source_message_id), enforced only when
-- source_message_id is present. A partial unique index (WHERE ...) keeps NULL
-- message ids out of the constraint, matching the SQLite behaviour.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_work_items_source
    ON work_items (source_channel, source_message_id)
    WHERE source_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS state_transitions (
    id         SERIAL PRIMARY KEY,
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

-- Ops observability heartbeat (NOT pipeline state). A single row per named
-- liveness signal; the harvest writes name='harvest' at the END of a completed
-- run (run.harvest -> store.set_last_harvest_at). The staleness watchdog reads
-- it once a day and alerts if it is older than the threshold. This table is
-- SEPARATE from work_items and does NOT participate in the state machine, so
-- writing it does NOT violate the advance()-is-sole-writer rule for work_items.
-- last_at / updated_at are UTC ISO-8601 strings, matching the schema's
-- datetime convention (TEXT, not timestamptz).
CREATE TABLE IF NOT EXISTS ops_heartbeat (
    name       TEXT PRIMARY KEY,
    last_at    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
