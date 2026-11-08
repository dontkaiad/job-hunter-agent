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
    -- Scoring-failure bookkeeping: an LLM outage DEFERS a vacancy, never deletes it.
    -- score_failed = exhausted its attempts; kept in 'extracted', skipped by harvest.
    score_attempts    INTEGER          NOT NULL DEFAULT 0,
    score_failed      BOOLEAN          NOT NULL DEFAULT FALSE,
    -- Why scoring is parked WITHOUT being a failure: 'link_only' (stub post, no
    -- text) or 'insufficient' (LLM said the posting lacks content). No attempt spent.
    defer_reason      TEXT,
    -- sha1 of the scorer's input (raw_text + fetched_text) when the model last judged the
    -- vacancy `insufficient`: it is NOT sent to the model again until this changes.
    defer_input_hash  TEXT,
    -- How the extraction was made (llm | heuristic:prefilter | heuristic:duplicate |
    -- heuristic:topic | heuristic:fallback = legacy, API failure before 2026-11-08) and
    -- the extract retry bookkeeping: an API/infra failure DEFERS extraction (item stays
    -- 'discovered'), after 3 failed tries extract_failed=TRUE: kept, never scored/deleted.
    extract_mode      TEXT,
    extract_attempts  INTEGER          NOT NULL DEFAULT 0,
    extract_failed    BOOLEAN          NOT NULL DEFAULT FALSE,
    -- Real page text fetched for a link-only stub (vacancy_fetch.py), its status
    -- (ok|denylisted|no_url|failed) and the number of failed fetch tries.
    fetched_text      TEXT,
    fetch_status      TEXT,
    fetch_attempts    INTEGER          NOT NULL DEFAULT 0,
    -- Canonical key of the vacancy's main link (vacancy_url.canonical_url): the
    -- one in the post body, and the final URL after fetching. Same key on two
    -- rows = the same vacancy posted twice (cross-channel duplicate).
    primary_url_key   TEXT,
    final_url_key     TEXT
);

-- Valid-state CHECK as a NAMED constraint, applied via idempotent drop+recreate
-- so it is the SINGLE source of truth AND widens an EXISTING DB (the inline
-- CREATE TABLE check only runs on a fresh table). The post-send funnel states
-- (screening/interview/offer/declined) were added here; widening is safe — it
-- never rejects existing rows. Keep in sync with job_hunter/states.ALL_STATES.
ALTER TABLE work_items DROP CONSTRAINT IF EXISTS work_items_state_check;
ALTER TABLE work_items ADD CONSTRAINT work_items_state_check CHECK (state IN (
    'discovered','extracted','scored','rejected','surfaced',
    'skipped','backlog','approved','researched','drafted','sent','closed',
    'screening','interview','offer','declined'
));

-- Why the operator skipped a vacancy (chips, several allowed; see skip_reasons.py).
-- reasons = comma-joined codes, '' = reviewed with no reason. source: dashboard
-- (the «Отклонить» reason chips).
CREATE TABLE IF NOT EXISTS skip_reasons (
    item_id    INTEGER PRIMARY KEY REFERENCES work_items(id) ON DELETE CASCADE,
    reasons    TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Shadow sample for the scoring eval: a deterministic 10% of the vacancies deleted for
-- scoring below min_persist_score are COPIED here (outside work_items, so no harvest,
-- dashboard or dedup query ever sees them) to be labelled later: without it the
-- below-the-cut region can never be measured. Capped by rows and expired by age.
CREATE TABLE IF NOT EXISTS eval_shadow (
    id              SERIAL PRIMARY KEY,
    orig_item_id    INTEGER NOT NULL UNIQUE,
    source_channel  TEXT,
    source_link     TEXT,
    raw_text        TEXT,
    fetched_text    TEXT,
    extracted_json  TEXT,
    score           DOUBLE PRECISION NOT NULL,
    primary_url_key TEXT,
    created_at      TEXT NOT NULL,
    -- 'score' = deleted for a model score under the cut; 'topic' = cut by the heuristic
    -- topic gate (measures the gate's recall). The row cap applies per kind.
    kind            TEXT NOT NULL DEFAULT 'score'
);

-- Golden set for the scoring eval (see job_hunter/golden.py, docs/CASES). Private: the
-- scores, the sample and the operator's labels live ONLY in the database.
-- eval_frame: every vacancy in the sampling frame scored on the CURRENT system.
CREATE TABLE IF NOT EXISTS eval_frame (
    item_id       INTEGER PRIMARY KEY,
    stratum       TEXT NOT NULL,             -- core | hard
    status        TEXT NOT NULL,             -- scored | insufficient | error
    current_score DOUBLE PRECISION,
    band          TEXT,
    judge_used    BOOLEAN,
    cost_usd      DOUBLE PRECISION,          -- measured from API usage
    scored_at     TEXT NOT NULL
);
-- eval_sample: the ordered cards of each labelling wave (controls repeat earlier cards).
CREATE TABLE IF NOT EXISTS eval_sample (
    id              SERIAL PRIMARY KEY,
    item_id         INTEGER NOT NULL,
    wave            INTEGER NOT NULL,
    order_no        INTEGER NOT NULL,
    stratum         TEXT NOT NULL,
    band            TEXT NOT NULL,
    band_source     TEXT,                     -- current (re-scored) | stored (older system's score)
    is_control      BOOLEAN NOT NULL,
    control_of_item INTEGER,
    held_out        BOOLEAN NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (wave, order_no)
);
-- eval_labels: blind labels. chips = comma-joined skip_reasons codes (only for 'no').
CREATE TABLE IF NOT EXISTS eval_labels (
    sample_id  INTEGER PRIMARY KEY REFERENCES eval_sample(id) ON DELETE CASCADE,
    label      TEXT NOT NULL CHECK (label IN ('yes', 'no', 'unsure')),
    chips      TEXT NOT NULL DEFAULT '',
    labeled_by TEXT,
    labeled_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Per-call token usage from the API `usage` block (cost measured, not modelled).
-- mode = 'sync' (Messages API) | 'batch' (Message Batches, billed at 50%).
CREATE TABLE IF NOT EXISTS llm_usage (
    id                 SERIAL PRIMARY KEY,
    ts                 TEXT NOT NULL,
    step               TEXT NOT NULL,
    model              TEXT NOT NULL,
    mode               TEXT NOT NULL DEFAULT 'sync',
    input_tokens       INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cache_read_tokens  INTEGER NOT NULL,
    output_tokens      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage (ts);

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
    created_at TEXT    NOT NULL,
    -- Provenance (added 2026-11-01, NOT back-filled: NULL = unknown/older row).
    -- channel: dashboard | bot | api | script | system. actor_id: the Telegram id of
    -- the person behind a dashboard/bot action (DB only), NULL for system rows.
    channel    TEXT,
    actor_id   TEXT
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

-- Harvest pause switch (NOT pipeline state). A single row, name='harvest',
-- toggled from the dashboard. When paused=true, run.harvest short-circuits
-- BEFORE ingest (no new posts fetched, no items processed, no notify) and
-- returns immediately -- the scheduled daily job and the one-shot `python -m
-- job_hunter.run` both honour it, since both call the same run.harvest. This
-- table is SEPARATE from work_items and does NOT participate in the state
-- machine (advance() remains the sole work_items writer). changed_at is a UTC
-- ISO-8601 string, matching the schema's datetime convention.
CREATE TABLE IF NOT EXISTS pipeline_control (
    name       TEXT PRIMARY KEY,
    paused     BOOLEAN NOT NULL DEFAULT FALSE,
    changed_at TEXT NOT NULL
);
