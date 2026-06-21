"""I/O: stdlib sqlite3 persistence (DESIGN.md §2, §3).

This is the ONLY module that touches the DB. Datetimes are written as UTC
ISO-8601 strings from clock.now_utc(). PRAGMA foreign_keys = ON on every
connection. ``pipeline.advance`` is the sole writer of ``state`` (it calls
``update_state`` + ``log_transition`` inside one transaction here).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from .clock import now_iso
from .states import DISCOVERED

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations", "schema.sql")


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
    def from_row(cls, row: sqlite3.Row) -> "WorkItem":
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


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with row factory and foreign keys enabled."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection, schema_path: str = _SCHEMA_PATH) -> None:
    """Create tables/indexes from migrations/schema.sql (idempotent)."""
    with open(schema_path, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()


def insert_item(
    conn: sqlite3.Connection,
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
    """
    ts = now_iso()
    try:
        cur = conn.execute(
            """
            INSERT INTO work_items
                (state, source_channel, source_link, source_message_id,
                 raw_text, extracted_json, relevance_score, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (state, source_channel, source_link, source_message_id, raw_text, ts, ts),
        )
    except sqlite3.IntegrityError:
        # Duplicate (source_channel, source_message_id).
        return None

    item_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO state_transitions
            (item_id, from_state, to_state, kind, actor, reason, created_at)
        VALUES (?, NULL, ?, 'deterministic', 'system', 'ingested', ?)
        """,
        (item_id, state, ts),
    )
    conn.commit()
    return item_id


def get_item(conn: sqlite3.Connection, item_id: int) -> Optional[WorkItem]:
    row = conn.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
    return WorkItem.from_row(row) if row else None


def list_by_state(conn: sqlite3.Connection, state: str, limit: int = 100) -> List[WorkItem]:
    rows = conn.execute(
        "SELECT * FROM work_items WHERE state = ? ORDER BY updated_at ASC LIMIT ?",
        (state, limit),
    ).fetchall()
    return [WorkItem.from_row(r) for r in rows]


def update_state(
    conn: sqlite3.Connection,
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
    sets = ["state = ?", "updated_at = ?"]
    params: list = [new_state, ts]
    if extracted_json is not None:
        sets.append("extracted_json = ?")
        params.append(extracted_json)
    if relevance_score is not None:
        sets.append("relevance_score = ?")
        params.append(relevance_score)
    params.append(item_id)

    try:
        conn.execute(f"UPDATE work_items SET {', '.join(sets)} WHERE id = ?", params)
        conn.execute(
            """
            INSERT INTO state_transitions
                (item_id, from_state, to_state, kind, actor, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (item_id, from_state, new_state, kind, actor, reason, ts),
        )
    except Exception:
        conn.rollback()
        raise
    if commit:
        conn.commit()


def set_extracted(
    conn: sqlite3.Connection, item_id: int, extracted_json: str, commit: bool = True
) -> None:
    """Persist extracted_json without changing state (used between T1 work and
    the state move). State changes still go through update_state."""
    conn.execute(
        "UPDATE work_items SET extracted_json = ?, updated_at = ? WHERE id = ?",
        (extracted_json, now_iso(), item_id),
    )
    if commit:
        conn.commit()


def list_transitions(conn: sqlite3.Connection, item_id: int) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM state_transitions WHERE item_id = ? ORDER BY id ASC",
        (item_id,),
    ).fetchall()


# --- Per-channel ingestion watermark (channel_state) ------------------------


def get_channel_watermark(conn: sqlite3.Connection, channel: str) -> Optional[datetime]:
    """Return the last processed post datetime for ``channel``, or None.

    None means the channel has no history (NEW channel). The stored value is a
    UTC ISO-8601 string; it is returned as a timezone-aware UTC datetime.
    """
    row = conn.execute(
        "SELECT last_post_at FROM channel_state WHERE channel = ?", (channel,)
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
    conn: sqlite3.Connection, channel: str, last_post_at: datetime, commit: bool = True
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
        VALUES (?, ?, ?)
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
