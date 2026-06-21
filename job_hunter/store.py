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
from .states import DISCOVERED

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


def connect(dsn: str) -> psycopg.Connection:
    """Open a psycopg3 connection from a DATABASE_URL (postgresql://...).

    ``row_factory=dict_row`` makes rows support ``row["col"]`` indexing, a
    drop-in replacement for the previous sqlite3.Row factory. psycopg3 is NOT
    autocommit: a transaction opens on the first execute and stays open until
    commit()/rollback() — matching the SQLite semantics advance() relies on.
    """
    return psycopg.connect(dsn, row_factory=dict_row)


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
