"""I/O: PostgreSQL (psycopg3) persistence (DESIGN.md §2, §3).

This is the ONLY module that touches the DB. Datetimes are written as UTC
ISO-8601 strings from clock.now_utc() (created_at/updated_at/last_post_at are
TEXT columns holding those strings — NOT timestamptz — preserving the exact
datetime convention the watermark comparison and now_iso() round-trip rely on).
``pipeline.advance`` is the sole writer of ``state`` (it calls ``update_state``
+ ``log_transition`` inside one transaction here).

Driver: synchronous psycopg v3 (package ``psycopg``). Connections are opened
with ``row_factory=dict_row`` so existing ``row["col"]`` access keeps working
exactly as it did with sqlite3.Row. The whole pipeline stays synchronous.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional, Tuple

import psycopg
from psycopg.rows import dict_row

from .clock import now_iso
from .states import DISCOVERED, EXTRACTED, SCORED, SURFACED

# PostgreSQL DDL run by init_db(). psycopg3 has no executescript, so the file is
# split into individual statements and executed one-by-one (all idempotent).
_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "migrations", "schema_pg.sql"
)


@dataclass
class WorkItem:
    id: int
    state: str
    source_channel: Optional[str]
    source_link: Optional[str]
    source_message_id: Optional[str]
    raw_text: Optional[str]
    extracted_json: Optional[str]
    relevance_score: Optional[float]
    created_at: str
    updated_at: str
    score_attempts: int = 0
    score_failed: bool = False
    defer_reason: Optional[str] = None
    defer_input_hash: Optional[str] = None
    fetched_text: Optional[str] = None
    fetch_status: Optional[str] = None
    fetch_attempts: int = 0
    primary_url_key: Optional[str] = None
    final_url_key: Optional[str] = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "WorkItem":
        return cls(
            id=row["id"],
            state=row["state"],
            source_channel=row["source_channel"],
            source_link=row["source_link"],
            source_message_id=row["source_message_id"],
            raw_text=row["raw_text"],
            extracted_json=row["extracted_json"],
            relevance_score=row["relevance_score"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            score_attempts=row.get("score_attempts") or 0,
            score_failed=bool(row.get("score_failed")),
            defer_reason=row.get("defer_reason"),
            defer_input_hash=row.get("defer_input_hash"),
            fetched_text=row.get("fetched_text"),
            fetch_status=row.get("fetch_status"),
            fetch_attempts=row.get("fetch_attempts") or 0,
            primary_url_key=row.get("primary_url_key"),
            final_url_key=row.get("final_url_key"),
        )


# --- DB resilience ----------------------------------------------------------

_RECONNECT_MAX_RETRIES = 3
_RECONNECT_BACKOFF_BASE = 2.0  # seconds; waits 2s, then 4s between attempts

# Connection-level errors that warrant a reconnect + retry.
_CONN_ERROR_TYPES = (psycopg.OperationalError, psycopg.InterfaceError)


def _raw_connect(dsn: str) -> psycopg.Connection:
    """Open a bare psycopg3 connection (testable seam; do not call directly)."""
    return psycopg.connect(dsn, row_factory=dict_row)


class ResilientConn:
    """Auto-reconnecting wrapper around a synchronous psycopg Connection.

    Returned by ``connect()``. All store functions receive this object as
    ``conn``.  On a connection-level error (AdminShutdown, OperationalError,
    InterfaceError) in ``execute()``, the wrapper reconnects ``_inner`` in-place
    and retries the statement once — callers see neither the disconnect nor the
    retry.

    ``_inner`` is replaced on reconnect so every caller that holds a reference
    to this wrapper automatically uses the new live connection.

    Public API matches the psycopg Connection subset used in this codebase:
    ``execute()``, ``commit()``, ``rollback()``, ``close()``, ``closed``.
    """

    def __init__(self, inner: psycopg.Connection, dsn: str) -> None:
        self._inner = inner
        self._dsn = dsn

    # ------------------------------------------------------------------
    # psycopg Connection proxy
    # ------------------------------------------------------------------

    @property
    def closed(self) -> int:
        return self._inner.closed

    def execute(self, sql: str, params=None):
        args: tuple = (sql,) if params is None else (sql, params)
        try:
            return self._inner.execute(*args)
        except _CONN_ERROR_TYPES as exc:
            print(
                f"[db] connection error: {exc!r} — reconnecting...",
                flush=True,
            )
            self._sync_reconnect()
            return self._inner.execute(*args)

    def commit(self) -> None:
        self._inner.commit()

    def rollback(self) -> None:
        try:
            self._inner.rollback()
        except Exception:
            pass  # if connection is already dead, rollback is moot

    def close(self) -> None:
        try:
            self._inner.close()
        except Exception:
            pass

    def cursor(self, *args, **kwargs):
        return self._inner.cursor(*args, **kwargs)

    # ------------------------------------------------------------------
    # Reconnect internals
    # ------------------------------------------------------------------

    def _sync_reconnect(self) -> None:
        """Replace _inner with a fresh connection, synchronously, with backoff.

        Prints to stdout (visible in ``docker logs``) on success or failure.
        The async path (``ensure_reconnected``) additionally logs to the ops
        Telegram channel.
        """
        import time as _t

        last_exc: Exception | None = None
        for attempt in range(1, _RECONNECT_MAX_RETRIES + 1):
            try:
                self._inner = _raw_connect(self._dsn)
                print(
                    f"[db] ⚠️ reconnected (attempt {attempt}/{_RECONNECT_MAX_RETRIES})",
                    flush=True,
                )
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < _RECONNECT_MAX_RETRIES:
                    _t.sleep(_RECONNECT_BACKOFF_BASE ** attempt)

        print(
            f"[db] 🔴 DB reconnect failed after {_RECONNECT_MAX_RETRIES} attempts:"
            f" {last_exc!r}",
            flush=True,
        )
        raise last_exc  # type: ignore[misc]


def connect(dsn: str) -> ResilientConn:
    """Open a resilient psycopg3 connection from a DATABASE_URL.

    Returns a ``ResilientConn`` that auto-reconnects on connection-level errors
    so any DB restart (AdminShutdown from a parallel project) is transparent to
    callers. ``row_factory=dict_row`` preserves ``row["col"]`` access throughout.

    psycopg3 is NOT autocommit: a transaction opens on the first execute and
    stays open until commit()/rollback() — matching the SQLite semantics
    advance() relies on.
    """
    return ResilientConn(_raw_connect(dsn), dsn)


async def ensure_reconnected(conn: ResilientConn, dsn: str) -> ResilientConn:
    """Proactive pre-flight reconnect for scheduled jobs and interactive callbacks.

    With ``ResilientConn`` in place, reactive reconnect happens automatically
    on any ``execute()`` call. This function is an ADDITIONAL proactive check:
    if ``conn.closed`` at the START of a job, reconnect immediately rather than
    failing on the first DB call. Also sends an ops notification via tg_logger
    (the only async touch-point).

    Callers continue to do ``conn = await store.ensure_reconnected(conn, dsn)``
    unchanged; the same ``ResilientConn`` object is returned (reconnected
    in-place).
    """
    from . import tg_logger

    if not conn.closed:
        return conn

    try:
        conn._sync_reconnect()
        await tg_logger.send_log(
            "⚠️ jobhunter: proactive DB reconnect before scheduled job "
            "(conn was closed — DB restart?)"
        )
    except Exception as exc:
        await tg_logger.send_log(
            f"🔴 jobhunter: proactive DB reconnect failed: {exc!r}"
        )
        raise

    return conn


def _split_statements(sql: str) -> List[str]:
    """Split a DDL script into individual statements on ';'. PURE.

    Comments are stripped FIRST (whole '--' lines are dropped) so a ';' that
    appears inside a comment cannot split a statement. The PG schema contains no
    functions / '$$' bodies, so splitting the comment-free text on ';' is safe.
    """
    # Drop full-line '--' comments before splitting (no ';' survives in a comment).
    code_lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    code = "\n".join(code_lines)
    out: List[str] = []
    for chunk in code.split(";"):
        stmt = chunk.strip()
        if stmt:
            out.append(stmt)
    return out


def record_llm_usage(conn, rec: dict) -> None:
    """Persist one API call's token usage (see llm.usage_record). Own commit."""
    from .clock import now_iso

    conn.execute(
        "INSERT INTO llm_usage (ts, step, model, mode, input_tokens, cache_write_tokens,"
        " cache_read_tokens, output_tokens) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (now_iso(), rec["step"], rec["model"], rec.get("mode", "sync"),
         rec["input_tokens"], rec["cache_write_tokens"], rec["cache_read_tokens"],
         rec["output_tokens"]),
    )
    conn.commit()


def make_usage_sink(dsn: str):
    """Sink for AnthropicClient: one short-lived connection per call (calls are
    seconds apart and may come from several threads, so no shared connection)."""
    def sink(rec: dict) -> None:
        conn = connect(dsn)
        try:
            record_llm_usage(conn, rec)
        finally:
            conn.close()
    return sink


def init_db(conn: psycopg.Connection, schema_path: str = _SCHEMA_PATH) -> None:
    """Create tables/indexes from migrations/schema_pg.sql (idempotent)."""
    with open(schema_path, "r", encoding="utf-8") as f:
        script = f.read()
    skip_state_check = _state_check_is_current(conn)
    for stmt in _split_statements(script):
        if skip_state_check and "work_items_state_check" in stmt:
            continue
        conn.execute(stmt)
    conn.commit()
    _ensure_score_failure_columns(conn)


def _state_check_is_current(conn: psycopg.Connection) -> bool:
    """True when work_items_state_check already allows exactly states.ALL_STATES.

    init_db runs on every ingest; DROP/ADD CONSTRAINT needs an ACCESS EXCLUSIVE
    lock, which blocks behind ANY connection idling in a transaction (a reader
    that never committed) and can deadlock a thread waiting on that reader.
    Only recreate the constraint when it really differs.
    """
    from .states import ALL_STATES

    row = conn.execute(
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conname = 'work_items_state_check' "
        "AND conrelid = to_regclass('work_items')"
    ).fetchone()
    conn.commit()  # never leave the catalog read transaction open
    if not row:
        return False
    return set(re.findall(r"'([a-z]+)'", row["def"])) == set(ALL_STATES)


def _ensure_score_failure_columns(conn: psycopg.Connection) -> None:
    """Add score_attempts/score_failed to a PRE-EXISTING work_items table.

    init_db runs on every ingest, and ALTER TABLE takes an ACCESS EXCLUSIVE lock
    even for ADD COLUMN IF NOT EXISTS, so only ALTER when a column is missing.
    A fresh table already has both from CREATE TABLE.
    """
    have = {
        r["column_name"]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'work_items'"
        ).fetchall()
    }
    conn.commit()  # end the read transaction; never hold it open across the ALTER
    for ddl in (
        ("score_attempts", "ALTER TABLE work_items ADD COLUMN IF NOT EXISTS "
                           "score_attempts INTEGER NOT NULL DEFAULT 0"),
        ("score_failed", "ALTER TABLE work_items ADD COLUMN IF NOT EXISTS "
                         "score_failed BOOLEAN NOT NULL DEFAULT FALSE"),
    ):
        if ddl[0] not in have:
            conn.execute(ddl[1])
            conn.commit()
    have_tr = {
        r["column_name"]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'state_transitions'"
        ).fetchall()
    }
    conn.commit()
    for col in ("channel", "actor_id"):
        if col not in have_tr:
            conn.execute(f"ALTER TABLE state_transitions ADD COLUMN IF NOT EXISTS {col} TEXT")
            conn.commit()
    for col, ddl in (
        ("defer_reason", "defer_reason TEXT"),
        ("defer_input_hash", "defer_input_hash TEXT"),
        ("fetched_text", "fetched_text TEXT"),
        ("fetch_status", "fetch_status TEXT"),
        ("fetch_attempts", "fetch_attempts INTEGER NOT NULL DEFAULT 0"),
        ("primary_url_key", "primary_url_key TEXT"),
        ("final_url_key", "final_url_key TEXT"),
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE work_items ADD COLUMN IF NOT EXISTS {ddl}")
            conn.commit()
    # Indexes AFTER the columns exist (init_db runs the schema script first, so an
    # index there would fail on an old table). Only build when missing: CREATE INDEX
    # blocks writes, and init_db runs on every ingest.
    have_idx = {
        r["indexname"]
        for r in conn.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = current_schema() AND tablename = 'work_items'"
        ).fetchall()
    }
    conn.commit()
    for name, col in (("idx_work_items_primary_url", "primary_url_key"),
                      ("idx_work_items_final_url", "final_url_key")):
        if name not in have_idx:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON work_items ({col})")
            conn.commit()


def insert_item(
    conn: psycopg.Connection,
    *,
    raw_text: str,
    source_channel: Optional[str] = None,
    source_link: Optional[str] = None,
    source_message_id: Optional[str] = None,
    state: str = DISCOVERED,
) -> Optional[int]:
    """Insert a new work item in ``discovered`` and log the initial event.

    Returns the new row id, or None if the row was a duplicate (dedup on
    (source_channel, source_message_id) when message id is present).

    On a UniqueViolation Postgres ABORTS the transaction, so we MUST rollback
    before returning None — otherwise every subsequent statement on this same
    connection fails with "current transaction is aborted".
    """
    ts = now_iso()
    try:
        row = conn.execute(
            """
            INSERT INTO work_items
                (state, source_channel, source_link, source_message_id,
                 raw_text, extracted_json, relevance_score, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, NULL, NULL, %s, %s)
            RETURNING id
            """,
            (state, source_channel, source_link, source_message_id, raw_text, ts, ts),
        ).fetchone()
    except psycopg.errors.UniqueViolation:
        # Duplicate (source_channel, source_message_id). Postgres aborts the
        # transaction on the error; roll back so the SAME connection stays usable.
        conn.rollback()
        return None

    item_id = row["id"]
    conn.execute(
        """
        INSERT INTO state_transitions
            (item_id, from_state, to_state, kind, actor, reason, created_at, channel)
        VALUES (%s, NULL, %s, 'deterministic', 'system', 'ingested', %s, 'system')
        """,
        (item_id, state, ts),
    )
    conn.commit()
    return item_id


def get_item(conn: psycopg.Connection, item_id: int) -> Optional[WorkItem]:
    row = conn.execute("SELECT * FROM work_items WHERE id = %s", (item_id,)).fetchone()
    return WorkItem.from_row(row) if row else None


def get_item_by_source(
    conn: psycopg.Connection, source_channel: str, source_message_id: str
) -> Optional[WorkItem]:
    """Look up an item by its (source_channel, source_message_id) dedup key.

    READ-ONLY. Used by the add-by-URL flow to detect a URL that is already in the
    pipeline (the SAME pair the partial unique index dedups on), so the caller can
    reply "already in pipeline" instead of inserting a second card. Returns None
    when no row matches.
    """
    row = conn.execute(
        "SELECT * FROM work_items "
        "WHERE source_channel = %s AND source_message_id = %s LIMIT 1",
        (source_channel, source_message_id),
    ).fetchone()
    return WorkItem.from_row(row) if row else None


def find_duplicate_by_source_link(
    conn: psycopg.Connection, source_link: Optional[str], exclude_id: int
) -> Optional[int]:
    """Return the id of another row sharing this exact ``source_link``, if any.

    READ-ONLY. Cross-SOURCE duplicate detector: the partial unique index only
    dedups within (source_channel, source_message_id), so the same posting
    mirrored by two different sources (e.g. the same job appearing via WWR AND
    Jobicy) is not caught there. Ordered by id ASC so the EARLIEST-ingested row
    is reported as canonical. Returns None when ``source_link`` is empty/None
    or no other row matches.
    """
    if not source_link:
        return None
    row = conn.execute(
        "SELECT id FROM work_items WHERE source_link = %s AND id != %s "
        "ORDER BY id ASC LIMIT 1",
        (source_link, exclude_id),
    ).fetchone()
    return row["id"] if row else None


def list_by_state(
    conn: psycopg.Connection,
    state: str,
    limit: int = 100,
    include_score_failed: bool = True,
) -> List[WorkItem]:
    """Items in ``state``, oldest-updated first. ``include_score_failed=False``
    hides items that exhausted their scoring attempts (harvest must not retry them)."""
    where = "state = %s" if include_score_failed else "state = %s AND NOT score_failed"
    rows = conn.execute(
        f"SELECT * FROM work_items WHERE {where} ORDER BY updated_at ASC LIMIT %s",
        (state, limit),
    ).fetchall()
    return [WorkItem.from_row(r) for r in rows]


def update_state(
    conn: psycopg.Connection,
    item_id: int,
    new_state: str,
    *,
    from_state: str,
    kind: str,
    actor: str,
    reason: Optional[str] = None,
    extracted_json: Optional[str] = None,
    relevance_score: Optional[float] = None,
    commit: bool = True,
    channel: Optional[str] = None,
    actor_id: Optional[str] = None,
) -> None:
    """Atomically update work_items.state (+ optional fields) and append a
    state_transitions row in the SAME transaction.

    ``channel`` / ``actor_id`` record WHO moved it: dashboard | bot | api | script |
    system. A human (hitl) decision made by code that did not say where it came from
    is recorded as ``script`` -- never silently as the dashboard or the bot.

    Only ``pipeline.advance`` should call this.
    """
    ts = now_iso()
    # Any state move resolves a parked item: clear its defer_reason.
    sets = ["state = %s", "updated_at = %s", "defer_reason = NULL", "defer_input_hash = NULL"]
    params: list = [new_state, ts]
    if extracted_json is not None:
        sets.append("extracted_json = %s")
        params.append(extracted_json)
    if relevance_score is not None:
        sets.append("relevance_score = %s")
        params.append(relevance_score)
    params.append(item_id)

    try:
        conn.execute(f"UPDATE work_items SET {', '.join(sets)} WHERE id = %s", params)
        conn.execute(
            """
            INSERT INTO state_transitions
                (item_id, from_state, to_state, kind, actor, reason, created_at,
                 channel, actor_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (item_id, from_state, new_state, kind, actor, reason, ts,
             channel or ("script" if kind == "hitl" else "system"),
             None if actor_id is None else str(actor_id)),
        )
    except Exception:
        conn.rollback()
        raise
    if commit:
        conn.commit()


def record_score_failure(
    conn: psycopg.Connection,
    item_id: int,
    *,
    max_attempts: int,
    reason: str,
    count_attempt: bool = True,
) -> Tuple[int, bool]:
    """Record a failed scoring attempt WITHOUT touching the item's state.

    Bumps ``score_attempts`` (unless ``count_attempt`` is False, e.g. an
    account-wide credits/auth outage that is not the vacancy's fault) and sets
    ``score_failed`` once ``max_attempts`` is reached. Appends an audit row.
    Returns (attempts, score_failed).
    """
    ts = now_iso()
    inc = 1 if count_attempt else 0
    try:
        row = conn.execute(
            """
            UPDATE work_items
               SET score_attempts = score_attempts + %s,
                   score_failed = (score_attempts + %s) >= %s,
                   updated_at = %s
             WHERE id = %s
         RETURNING state, score_attempts, score_failed
            """,
            (inc, inc, max_attempts, ts, item_id),
        ).fetchone()
        if row is None:
            conn.rollback()
            return 0, False
        conn.execute(
            """
            INSERT INTO state_transitions
                (item_id, from_state, to_state, kind, actor, reason, created_at, channel)
            VALUES (%s, %s, %s, 'deterministic', 'system', %s, %s, 'system')
            """,
            (item_id, row["state"], row["state"], reason, ts),
        )
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return row["score_attempts"], bool(row["score_failed"])


def set_defer_reason(
    conn: psycopg.Connection,
    item_id: int,
    reason: Optional[str],
    *,
    audit_reason: Optional[str] = None,
    reset_attempts: bool = False,
    input_hash: Optional[str] = None,
) -> bool:
    """Park an item (``link_only`` / ``insufficient``) or un-park it (None).

    Does NOT spend a score attempt and never touches state or score. With
    ``reset_attempts`` also zeroes score_attempts/score_failed (one-off repair).
    ``input_hash`` (insufficient only) records what the model read, so the item is not
    re-sent until that input changes. Writes an audit row only when the reason actually
    changes. Returns True when something changed.
    """
    ts = now_iso()
    new_hash = input_hash if reason == "insufficient" else None
    try:
        row = conn.execute(
            "SELECT state, defer_reason, defer_input_hash, score_attempts, score_failed "
            "FROM work_items WHERE id = %s", (item_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False
        reason_changed = row["defer_reason"] != reason
        changed = reason_changed or row["defer_input_hash"] != new_hash or (
            reset_attempts and (row["score_attempts"] or row["score_failed"])
        )
        if not changed:
            conn.rollback()
            return False
        if reset_attempts:
            conn.execute(
                "UPDATE work_items SET defer_reason = %s, defer_input_hash = %s, score_attempts = 0, "
                "score_failed = FALSE, updated_at = %s WHERE id = %s",
                (reason, new_hash, ts, item_id),
            )
        else:
            conn.execute(
                "UPDATE work_items SET defer_reason = %s, defer_input_hash = %s, updated_at = %s WHERE id = %s",
                (reason, new_hash, ts, item_id),
            )
        if reason_changed or reset_attempts:
            conn.execute(
                """
                INSERT INTO state_transitions
                    (item_id, from_state, to_state, kind, actor, reason, created_at, channel)
                VALUES (%s, %s, %s, 'deterministic', 'system', %s, %s, 'system')
                """,
                (item_id, row["state"], row["state"],
                 audit_reason or f"score parked: {reason}", ts),
            )
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return True


def set_input_hash(conn: psycopg.Connection, item_id: int, input_hash: str) -> None:
    """Record the scorer-input hash of an already-parked `insufficient` item (one-off
    repair). Touches nothing else, not even updated_at (the harvest queue order)."""
    conn.execute(
        "UPDATE work_items SET defer_input_hash = %s WHERE id = %s AND defer_reason = 'insufficient'",
        (input_hash, item_id),
    )
    conn.commit()


def set_url_keys(
    conn: psycopg.Connection,
    item_id: int,
    *,
    primary: Optional[str] = None,
    final: Optional[str] = None,
    commit: bool = True,
) -> None:
    """Store the canonical vacancy-URL keys (only the ones given). No audit row."""
    sets, params = [], []
    if primary is not None:
        sets.append("primary_url_key = %s")
        params.append(primary)
    if final is not None:
        sets.append("final_url_key = %s")
        params.append(final)
    if not sets:
        return
    params.append(item_id)
    conn.execute(f"UPDATE work_items SET {', '.join(sets)} WHERE id = %s", params)
    if commit:
        conn.commit()


def find_duplicate_by_url_keys(
    conn: psycopg.Connection, keys: List[str], item_id: int
) -> Optional[int]:
    """Id of an OLDER row (any state) whose primary/final URL key matches one of
    ``keys``. READ-ONLY. Only older ids count, so two copies can never both be
    judged the duplicate of each other: the earliest-ingested row is canonical."""
    keys = [k for k in keys if k]
    if not keys:
        return None
    row = conn.execute(
        "SELECT id FROM work_items WHERE id < %s "
        "AND (primary_url_key = ANY(%s) OR final_url_key = ANY(%s)) "
        "ORDER BY id ASC LIMIT 1",
        (item_id, keys, keys),
    ).fetchone()
    return row["id"] if row else None


def record_fetch(
    conn: psycopg.Connection,
    item_id: int,
    status: str,
    *,
    text: Optional[str] = None,
    url: Optional[str] = None,
    count_attempt: bool = False,
) -> int:
    """Persist the outcome of fetching a stub's link. Never touches state/score.

    ``status`` is ok|denylisted|no_url|failed. ``count_attempt`` bumps
    fetch_attempts (used for real failed requests only). Returns fetch_attempts.
    """
    ts = now_iso()
    try:
        row = conn.execute(
            """
            UPDATE work_items
               SET fetch_status = %s,
                   fetched_text = COALESCE(%s, fetched_text),
                   fetch_attempts = fetch_attempts + %s,
                   updated_at = %s
             WHERE id = %s
         RETURNING state, fetch_attempts
            """,
            (status, text, 1 if count_attempt else 0, ts, item_id),
        ).fetchone()
        if row is None:
            conn.rollback()
            return 0
        host = url.split("/")[2] if url and "//" in url else "-"
        conn.execute(
            """
            INSERT INTO state_transitions
                (item_id, from_state, to_state, kind, actor, reason, created_at, channel)
            VALUES (%s, %s, %s, 'deterministic', 'system', %s, %s, 'system')
            """,
            (item_id, row["state"], row["state"],
             f"fetch {status} host={host} chars={len(text) if text else 0}", ts),
        )
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return row["fetch_attempts"]


def get_skip_reasons(conn: psycopg.Connection, item_id: int) -> Optional[List[str]]:
    """Reason codes recorded for a skipped item; None when never reviewed."""
    from . import skip_reasons

    row = conn.execute(
        "SELECT reasons FROM skip_reasons WHERE item_id = %s", (item_id,)
    ).fetchone()
    return None if row is None else skip_reasons.decode(row["reasons"])


def set_skip_reasons(
    conn: psycopg.Connection, item_id: int, codes: List[str], source: str
) -> None:
    """Upsert the reason set (the original ``source`` is kept on update)."""
    from . import skip_reasons

    ts = now_iso()
    conn.execute(
        """
        INSERT INTO skip_reasons (item_id, reasons, source, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (item_id) DO UPDATE
           SET reasons = EXCLUDED.reasons, updated_at = EXCLUDED.updated_at
        """,
        (item_id, skip_reasons.encode(codes), source, ts, ts),
    )
    conn.commit()


SHADOW_SALT = "shadow-v1"


def shadow_selected(item_id: int, rate_percent: int) -> bool:
    """Deterministic share of ids chosen for the shadow sample. PURE.

    sha1("<id>:shadow-v1") mod 100 < rate: the same id always gives the same answer
    (re-runs, retries and reprocessing never change the sample) and no RNG state is kept.
    """
    if rate_percent <= 0:
        return False
    import hashlib

    h = int(hashlib.sha1(f"{item_id}:{SHADOW_SALT}".encode()).hexdigest(), 16)
    return h % 100 < rate_percent


def shadow_copy(
    conn: psycopg.Connection,
    item: WorkItem,
    score: float,
    *,
    rate_percent: int,
    max_rows: int,
    ttl_days: int,
) -> bool:
    """Copy a vacancy about to be deleted for scoring below the cut into eval_shadow.

    Returns True when a row was added. Expired rows (older than ``ttl_days``) are
    purged first; when ``max_rows`` are still held nothing more is added (the earliest
    stay). Never touches work_items. The caller still deletes the item either way.
    """
    if not shadow_selected(item.id, rate_percent):
        return False
    from datetime import datetime, timedelta

    ts = now_iso()
    try:
        cutoff = (datetime.fromisoformat(ts) - timedelta(days=ttl_days)).isoformat()
        conn.execute("DELETE FROM eval_shadow WHERE created_at < %s", (cutoff,))
        held = conn.execute("SELECT count(*) AS n FROM eval_shadow").fetchone()["n"]
        if held >= max_rows:
            conn.commit()
            return False
        cur = conn.execute(
            """
            INSERT INTO eval_shadow
                (orig_item_id, source_channel, source_link, raw_text, fetched_text,
                 extracted_json, score, primary_url_key, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (orig_item_id) DO NOTHING
            """,
            (item.id, item.source_channel, item.source_link, item.raw_text,
             item.fetched_text, item.extracted_json, float(score), item.primary_url_key, ts),
        )
        conn.commit()
        return cur.rowcount == 1
    except Exception:
        conn.rollback()
        raise


def delete_item(conn: psycopg.Connection, item_id: int) -> None:
    """Delete a work_item and its transition audit log.

    Used to drop low-score vacancies after scoring so they never persist in a
    terminal state (rejected). The item briefly existed in discovered/extracted/
    scored states during processing — this cleans it up completely.
    """
    conn.execute("DELETE FROM state_transitions WHERE item_id = %s", (item_id,))
    conn.execute("DELETE FROM work_items WHERE id = %s", (item_id,))
    conn.commit()


def set_extracted(
    conn: psycopg.Connection, item_id: int, extracted_json: str, commit: bool = True
) -> None:
    """Persist extracted_json without changing state (used between T1 work and
    the state move). State changes still go through update_state."""
    conn.execute(
        "UPDATE work_items SET extracted_json = %s, updated_at = %s WHERE id = %s",
        (extracted_json, now_iso(), item_id),
    )
    if commit:
        conn.commit()


def list_transitions(conn: psycopg.Connection, item_id: int) -> List[Mapping[str, Any]]:
    return conn.execute(
        "SELECT * FROM state_transitions WHERE item_id = %s ORDER BY id ASC",
        (item_id,),
    ).fetchall()


# --- Read-only dashboard queries (issue #3) ---------------------------------
# Additive, pure-read helpers for the READ-ONLY FastAPI dashboard. They NEVER
# write/commit and do NOT change any state. Column-backed filters (state /
# relevance_score) are applied in SQL here; extracted_json-backed filters
# (remote, free-text q) live in the API layer because extracted_json is a TEXT
# JSON blob, not jsonb — so jsonb operators are not used.

# "processed" partition (разобрано / неразобрано). UNPROCESSED = still in the
# review inbox / automated pipeline; PROCESSED = a human acted or the item is
# resolved. This is a judgment call (see webapi.py) the user can tweak.
_UNPROCESSED_STATES = (DISCOVERED, EXTRACTED, SCORED, SURFACED)


def list_pipeline(
    conn: psycopg.Connection,
    *,
    status: Optional[str] = None,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    processed: Optional[bool] = None,
    limit: int = 1000,
) -> List[WorkItem]:
    """Return work items for the dashboard, SQL-side filtered (READ-ONLY).

    Only COLUMN-backed filters are applied here:
      - ``status``: exact ``state = %s``.
      - ``min_score``: ``relevance_score >= %s``. Rows with a NULL score are
        EXCLUDED when ``min_score`` is set (a NULL score cannot satisfy a
        numeric floor).
      - ``max_score``: ``relevance_score < %s`` — a HALF-OPEN upper bound (the
        max itself is EXCLUDED). Rows with a NULL score are EXCLUDED when
        ``max_score`` is set (a NULL score cannot satisfy a numeric ceiling).
      - ``processed``: partition over ``state`` (see ``_UNPROCESSED_STATES``).

    ``min_score`` and ``max_score`` compose into the half-open band
    ``[min_score, max_score)`` (score >= min AND score < max). Each is fully
    optional and independent; passing only one applies just that bound, and
    passing neither leaves behavior identical to before this param existed.

    remote / free-text ``q`` filtering is intentionally NOT done here (they need
    the parsed extracted_json blob) — the API layer applies them in Python.

    Ordered by relevance_score DESC (NULLS LAST), then created_at DESC, so the
    most relevant, most recent items come first. No writes, no commit.
    """
    clauses: List[str] = []
    params: list = []

    if status is not None:
        clauses.append("state = %s")
        params.append(status)

    if min_score is not None:
        clauses.append("relevance_score IS NOT NULL AND relevance_score >= %s")
        params.append(float(min_score))

    if max_score is not None:
        clauses.append("relevance_score IS NOT NULL AND relevance_score < %s")
        params.append(float(max_score))

    if processed is not None:
        placeholders = ", ".join(["%s"] * len(_UNPROCESSED_STATES))
        if processed:
            clauses.append(f"state NOT IN ({placeholders})")
        else:
            clauses.append(f"state IN ({placeholders})")
        params.extend(_UNPROCESSED_STATES)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    rows = conn.execute(
        "SELECT * FROM work_items"
        + where
        + " ORDER BY relevance_score DESC NULLS LAST, created_at DESC LIMIT %s",
        params,
    ).fetchall()
    return [WorkItem.from_row(r) for r in rows]


# --- Per-channel ingestion watermark (channel_state) ------------------------


def get_channel_watermark(conn: psycopg.Connection, channel: str) -> Optional[datetime]:
    """Return the last processed post datetime for ``channel``, or None.

    None means the channel has no history (NEW channel). The stored value is a
    UTC ISO-8601 string; it is returned as a timezone-aware UTC datetime.
    """
    row = conn.execute(
        "SELECT last_post_at FROM channel_state WHERE channel = %s", (channel,)
    ).fetchone()
    if row is None or row["last_post_at"] is None:
        return None
    try:
        dt = datetime.fromisoformat(row["last_post_at"])
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def set_channel_watermark(
    conn: psycopg.Connection, channel: str, last_post_at: datetime, commit: bool = True
) -> None:
    """Upsert the per-channel watermark (newest processed post datetime).

    ``last_post_at`` must be timezone-aware; it is stored as a UTC ISO-8601
    string. Never moves the watermark backwards.
    """
    iso = last_post_at.astimezone(timezone.utc).isoformat()
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO channel_state (channel, last_post_at, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT(channel) DO UPDATE SET
            last_post_at = CASE
                WHEN excluded.last_post_at > channel_state.last_post_at
                     OR channel_state.last_post_at IS NULL
                THEN excluded.last_post_at
                ELSE channel_state.last_post_at
            END,
            updated_at = excluded.updated_at
        """,
        (channel, iso, ts),
    )
    if commit:
        conn.commit()


# --- Ops observability heartbeat (ops_heartbeat) ----------------------------
#
# Additive, OBSERVABILITY-ONLY helpers. They write the SEPARATE ops_heartbeat
# table, never work_items — so they do NOT violate the rule that pipeline.advance
# is the sole writer of work_items state. Mirrors the channel_state watermark
# patterns above (parameterized %s, UTC ISO-8601 TEXT, tz-aware parse on read).

_HARVEST_HEARTBEAT_NAME = "harvest"


def set_last_harvest_at(
    conn: psycopg.Connection, dt: datetime, commit: bool = True
) -> None:
    """UPSERT the harvest heartbeat (name='harvest') to ``dt``.

    ``dt`` must be timezone-aware; it is stored as a UTC ISO-8601 string in the
    OPS heartbeat table (NOT work_items). ``updated_at`` records when the row was
    touched. Observability write only; advance() remains the sole work_items
    writer.
    """
    last_iso = dt.astimezone(timezone.utc).isoformat()
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO ops_heartbeat (name, last_at, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT(name) DO UPDATE SET
            last_at = excluded.last_at,
            updated_at = excluded.updated_at
        """,
        (_HARVEST_HEARTBEAT_NAME, last_iso, ts),
    )
    if commit:
        conn.commit()


def get_last_harvest_at(conn: psycopg.Connection) -> Optional[datetime]:
    """Return the last completed-harvest datetime (UTC, tz-aware), or None.

    None means no harvest has completed yet (no heartbeat row) — the staleness
    watchdog treats that as "no baseline" and does NOT alert.
    """
    row = conn.execute(
        "SELECT last_at FROM ops_heartbeat WHERE name = %s",
        (_HARVEST_HEARTBEAT_NAME,),
    ).fetchone()
    if row is None or row["last_at"] is None:
        return None
    try:
        dt = datetime.fromisoformat(row["last_at"])
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --- Harvest pause switch (pipeline_control) ---------------------------------
#
# Additive, OBSERVABILITY/CONTROL-ONLY helpers. They write the SEPARATE
# pipeline_control table, never work_items -- so they do NOT violate the rule
# that pipeline.advance is the sole writer of work_items state. Mirrors the
# ops_heartbeat single-row-per-name pattern above.

_HARVEST_CONTROL_NAME = "harvest"


def set_pipeline_paused(
    conn: psycopg.Connection, paused: bool, commit: bool = True
) -> None:
    """UPSERT the harvest pause switch (name='harvest') to ``paused``.

    ``changed_at`` records when the row was touched (UTC ISO-8601), so the
    dashboard can show "paused since <date>" / "active since <date>". Control
    write only; advance() remains the sole work_items writer.
    """
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO pipeline_control (name, paused, changed_at)
        VALUES (%s, %s, %s)
        ON CONFLICT(name) DO UPDATE SET
            paused = excluded.paused,
            changed_at = excluded.changed_at
        """,
        (_HARVEST_CONTROL_NAME, paused, ts),
    )
    if commit:
        conn.commit()


def get_pipeline_pause_state(
    conn: psycopg.Connection,
) -> Tuple[bool, Optional[datetime]]:
    """Return (paused, changed_at) for the harvest pause switch.

    No row (never toggled) means NOT paused, with changed_at=None -- the
    default, pre-toggle state.
    """
    row = conn.execute(
        "SELECT paused, changed_at FROM pipeline_control WHERE name = %s",
        (_HARVEST_CONTROL_NAME,),
    ).fetchone()
    if row is None:
        return False, None
    changed_at: Optional[datetime]
    try:
        changed_at = datetime.fromisoformat(row["changed_at"])
    except (ValueError, TypeError):
        changed_at = None
    if changed_at is not None:
        if changed_at.tzinfo is None:
            changed_at = changed_at.replace(tzinfo=timezone.utc)
        changed_at = changed_at.astimezone(timezone.utc)
    return bool(row["paused"]), changed_at
