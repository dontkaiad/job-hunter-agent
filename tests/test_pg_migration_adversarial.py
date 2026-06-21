"""Adversarial validation tests for the SQLite->PostgreSQL migration.

These tests cover GAPS identified during validation that were not covered by the
existing 418-test suite:

  1. CHECK constraint rejects bad state on a direct INSERT (not just UPDATE).
  2. Watermark equal-timestamp is a no-op (no regression, no change to last_post_at).
  3. advance() atomicity: if the state_transitions INSERT is made to fail artificially,
     the preceding work_items UPDATE must also be rolled back.
  4. insert_item with a custom bad state leaves the connection wedged (CheckViolation
     NOT caught by the UniqueViolation-only try/except in insert_item). This documents
     the gap: insert_item only catches UniqueViolation; any other error leaves the conn
     in an aborted state. In production this cannot be triggered (state param defaults
     to DISCOVERED and no caller overrides it), but the behavior is tested here.
  5. set_extracted is dead code (confirmed: not called from any production module).
     Tested here as a pure behavioral sanity check.
  6. NULL watermark initial state: get_channel_watermark on a new channel returns None
     before any watermark is set (redundant but explicit).
  7. Two NULL source_message_ids in the SAME channel do NOT collide (partial index).
  8. Same source_message_id in different channels does NOT deduplicate (index is on
     (source_channel, source_message_id) together).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import psycopg
import pytest

from job_hunter import store
from job_hunter.states import DISCOVERED, EXTRACTED, KIND_DETERMINISTIC


# ---------------------------------------------------------------------------
# 1. CHECK constraint rejects a bad state on direct INSERT
# ---------------------------------------------------------------------------

def test_check_constraint_rejects_bad_state_on_insert(conn):
    """The CHECK (state IN (...)) constraint must fire on INSERT with an illegal
    state value, raising psycopg.errors.CheckViolation."""
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            """
            INSERT INTO work_items
                (state, source_channel, source_link, source_message_id,
                 raw_text, extracted_json, relevance_score, created_at, updated_at)
            VALUES (%s, %s, NULL, NULL, %s, NULL, NULL, %s, %s)
            """,
            ("ILLEGAL_STATE", "@c", "some text",
             "2026-06-20T10:00:00+00:00", "2026-06-20T10:00:00+00:00"),
        )
    # The connection is now in an aborted state -- rollback restores it.
    conn.rollback()
    # After rollback the connection must be usable again.
    conn.execute("SELECT 1")


# ---------------------------------------------------------------------------
# 2. Watermark equal-timestamp is a no-op (no backwards regression)
# ---------------------------------------------------------------------------

def test_watermark_equal_timestamp_is_noop(conn):
    """Setting a watermark equal to the current value must NOT change last_post_at
    (lexicographic comparison: excluded > current is False for equal strings).
    This complements test_channel_watermark_never_goes_backwards which tests a
    strictly earlier timestamp."""
    channel = "@equal_test"
    dt = datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)

    # Set initial watermark.
    store.set_channel_watermark(conn, channel, dt)
    first = store.get_channel_watermark(conn, channel)
    assert first == dt

    # Set the SAME timestamp again.
    store.set_channel_watermark(conn, channel, dt)
    second = store.get_channel_watermark(conn, channel)

    # last_post_at must be unchanged (no regression, no spurious update).
    assert second == first, (
        f"Setting equal watermark must not change last_post_at; "
        f"before={first!r}, after={second!r}"
    )


# ---------------------------------------------------------------------------
# 3. advance() / update_state atomicity under artificial failure
# ---------------------------------------------------------------------------

def test_update_state_rollback_when_transition_insert_fails(conn):
    """If the INSERT into state_transitions fails after the UPDATE to work_items
    has already executed, the exception handler in update_state must call
    conn.rollback(), so the work_items UPDATE is also undone.

    We simulate the failure by monkey-patching conn.execute to raise on the
    second call (the state_transitions INSERT) while letting the first call
    (the work_items UPDATE) succeed.
    """
    item_id = store.insert_item(conn, raw_text="x", source_channel="@c",
                                source_message_id="atom_fail")
    assert item_id is not None
    # Confirm starting state.
    assert store.get_item(conn, item_id).state == DISCOVERED

    original_execute = conn.execute
    call_count = {"n": 0}

    def patched_execute(query, params=None, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            # The second execute inside update_state is the state_transitions INSERT.
            raise psycopg.errors.DivisionByZero("simulated failure in INSERT")
        return original_execute(query, params, *args, **kwargs)

    conn.execute = patched_execute  # type: ignore[assignment]
    try:
        with pytest.raises(psycopg.errors.DivisionByZero):
            store.update_state(
                conn, item_id, EXTRACTED,
                from_state=DISCOVERED, kind=KIND_DETERMINISTIC, actor="system",
                reason="should roll back",
            )
    finally:
        conn.execute = original_execute  # type: ignore[assignment]

    # update_state's except block must have called conn.rollback().
    # Verify: the work_items state must still be DISCOVERED (the UPDATE was rolled back).
    item = store.get_item(conn, item_id)
    assert item.state == DISCOVERED, (
        f"work_items.state must be DISCOVERED after rolled-back update_state; "
        f"got {item.state!r}"
    )

    # The state_transitions table must have NO row for EXTRACTED.
    trans = store.list_transitions(conn, item_id)
    to_states = [t["to_state"] for t in trans]
    assert EXTRACTED not in to_states, (
        f"state_transitions must have no EXTRACTED row after rollback; got {to_states}"
    )

    # The connection must still be usable after the rollback.
    items = store.list_by_state(conn, DISCOVERED)
    assert any(i.id == item_id for i in items)


# ---------------------------------------------------------------------------
# 4. insert_item with a bad state= value: CheckViolation is NOT caught,
#    leaving the connection in an aborted state (documents the gap)
# ---------------------------------------------------------------------------

def test_insert_item_bad_state_raises_and_wedges_connection(conn):
    """insert_item only catches psycopg.errors.UniqueViolation; any other error
    (including CheckViolation from a bad state value) propagates to the caller
    and leaves the connection in an aborted (wedged) state.

    In production this cannot be triggered: the state parameter defaults to
    DISCOVERED and no external caller overrides it. This test documents the
    invariant: if insert_item ever raises non-UniqueViolation, the caller is
    responsible for rolling back.

    Note: this does NOT indicate a production bug (the path is unreachable), but
    it confirms the documented behaviour so any future change that adds a state=
    override caller must also add a rollback guard.
    """
    with pytest.raises(psycopg.errors.CheckViolation):
        store.insert_item(
            conn,
            raw_text="test",
            source_channel="@c",
            source_message_id="bad_state_1",
            state="NOT_A_VALID_STATE",  # triggers CheckViolation
        )
    # Connection is now aborted; the caller must rollback.
    conn.rollback()
    # After rollback the connection is usable again.
    good_id = store.insert_item(conn, raw_text="ok", source_channel="@c",
                                source_message_id="bad_state_after_rollback")
    assert good_id is not None


# ---------------------------------------------------------------------------
# 5. set_extracted dead-code sanity: the function works if called
# ---------------------------------------------------------------------------

def test_set_extracted_updates_extracted_json(conn):
    """store.set_extracted is defined but currently unused in production code
    (no module calls it). This test confirms the function itself works
    correctly, guarding against a stale no-op that would silently do nothing
    if it were ever called.
    """
    item_id = store.insert_item(conn, raw_text="x", source_channel="@c",
                                source_message_id="se_test")
    assert item_id is not None

    # Initially extracted_json is NULL.
    assert store.get_item(conn, item_id).extracted_json is None

    store.set_extracted(conn, item_id, '{"title": "Test"}')
    item = store.get_item(conn, item_id)
    assert item.extracted_json == '{"title": "Test"}'


# ---------------------------------------------------------------------------
# 6. get_channel_watermark on a new channel returns None
# ---------------------------------------------------------------------------

def test_get_channel_watermark_new_channel_is_none(conn):
    """A channel that has never had a watermark set must return None.
    Explicit confirmation of the no-row case, separate from the roundtrip test."""
    result = store.get_channel_watermark(conn, "@brand_new_channel")
    assert result is None


# ---------------------------------------------------------------------------
# 7. Two NULL source_message_ids in the same channel: no collision
#    (partial unique index WHERE source_message_id IS NOT NULL)
# ---------------------------------------------------------------------------

def test_two_null_message_ids_same_channel_do_not_collide(conn):
    """The partial unique index enforces uniqueness ONLY when source_message_id
    IS NOT NULL. Two rows with NULL message_id in the same channel must NOT
    trigger a UniqueViolation."""
    a = store.insert_item(conn, raw_text="post A", source_channel="@same",
                          source_message_id=None)
    b = store.insert_item(conn, raw_text="post B", source_channel="@same",
                          source_message_id=None)
    assert a is not None, "first NULL-id insert must succeed"
    assert b is not None, "second NULL-id insert must succeed (nulls distinct)"
    assert a != b, "two NULL-id rows must get different ids"


# ---------------------------------------------------------------------------
# 8. Same source_message_id in different channels: no collision
# ---------------------------------------------------------------------------

def test_same_message_id_different_channels_not_deduplicated(conn):
    """The unique index is on (source_channel, source_message_id). Two rows with
    the same message_id but different channels must NOT collide."""
    x = store.insert_item(conn, raw_text="post", source_channel="@chan_a",
                          source_message_id="MSG100")
    y = store.insert_item(conn, raw_text="post", source_channel="@chan_b",
                          source_message_id="MSG100")
    assert x is not None
    assert y is not None
    assert x != y


# ---------------------------------------------------------------------------
# 9. Partial unique index: same (channel, message_id) pair DOES collide
# ---------------------------------------------------------------------------

def test_same_channel_and_message_id_collides(conn):
    """The partial unique index must enforce uniqueness when source_message_id
    IS NOT NULL. Inserting the same (channel, message_id) pair twice must
    return None on the second attempt (UniqueViolation caught + rolled back)."""
    first = store.insert_item(conn, raw_text="original", source_channel="@dup_chan",
                              source_message_id="DUP42")
    assert first is not None

    second = store.insert_item(conn, raw_text="duplicate", source_channel="@dup_chan",
                               source_message_id="DUP42")
    assert second is None, "duplicate (channel, message_id) must return None"

    # Connection must be usable after the dedup rollback.
    third = store.insert_item(conn, raw_text="new", source_channel="@dup_chan",
                              source_message_id="NEW99")
    assert third is not None
