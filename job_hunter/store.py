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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional

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


def init_db(conn: psycopg.Connection, schema_path: str = _SCHEMA_PATH) -> None:
    """Create tables/indexes from migrations/schema_pg.sql (idempotent)."""
    with open(schema_path, "r", encoding="utf-8") as f:
        script = f.read()
    for stmt in _split_statements(script):
        conn.execute(stmt)
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
            (item_id, from_state, to_state, kind, actor, reason, created_at)
        VALUES (%s, NULL, %s, 'deterministic', 'system', 'ingested', %s)
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


def list_by_state(conn: psycopg.Connection, state: str, limit: int = 100) -> List[WorkItem]:
    rows = conn.execute(
        "SELECT * FROM work_items WHERE state = %s ORDER BY updated_at ASC LIMIT %s",
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
) -> None:
    """Atomically update work_items.state (+ optional fields) and append a
    state_transitions row in the SAME transaction.

    Only ``pipeline.advance`` should call this.
    """
    ts = now_iso()
    sets = ["state = %s", "updated_at = %s"]
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
                (item_id, from_state, to_state, kind, actor, reason, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (item_id, from_state, new_state, kind, actor, reason, ts),
        )
    except Exception:
        conn.rollback()
        raise
    if commit:
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
