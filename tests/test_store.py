"""CRUD + transition logging + UTC strings (in-memory sqlite)."""

from datetime import datetime

from job_hunter import store
from job_hunter.states import DISCOVERED, EXTRACTED, KIND_DETERMINISTIC


def test_insert_creates_item_and_initial_transition(conn):
    item_id = store.insert_item(conn, raw_text="hello", source_channel="@c",
                                source_message_id="1")
    assert item_id is not None
    item = store.get_item(conn, item_id)
    assert item.state == DISCOVERED
    assert item.raw_text == "hello"

    trans = store.list_transitions(conn, item_id)
    assert len(trans) == 1
    assert trans[0]["from_state"] is None
    assert trans[0]["to_state"] == DISCOVERED


def test_timestamps_are_utc_offset_aware(conn):
    item_id = store.insert_item(conn, raw_text="x", source_channel="@c", source_message_id="9")
    item = store.get_item(conn, item_id)
    dt = datetime.fromisoformat(item.created_at)
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_dedup_on_channel_and_message_id(conn):
    a = store.insert_item(conn, raw_text="x", source_channel="@c", source_message_id="42")
    b = store.insert_item(conn, raw_text="x again", source_channel="@c", source_message_id="42")
    assert a is not None
    assert b is None  # duplicate skipped


def test_null_message_id_not_deduped(conn):
    a = store.insert_item(conn, raw_text="x", source_channel="@c", source_message_id=None)
    b = store.insert_item(conn, raw_text="y", source_channel="@c", source_message_id=None)
    assert a is not None and b is not None and a != b


def test_update_state_atomic_and_logs(conn):
    item_id = store.insert_item(conn, raw_text="x", source_channel="@c", source_message_id="1")
    store.update_state(conn, item_id, EXTRACTED, from_state=DISCOVERED,
                       kind=KIND_DETERMINISTIC, actor="system", reason="t1",
                       extracted_json='{"title":"x"}')
    item = store.get_item(conn, item_id)
    assert item.state == EXTRACTED
    assert item.extracted_json == '{"title":"x"}'
    trans = store.list_transitions(conn, item_id)
    assert [t["to_state"] for t in trans] == [DISCOVERED, EXTRACTED]


def test_list_by_state(conn):
    store.insert_item(conn, raw_text="a", source_channel="@c", source_message_id="1")
    store.insert_item(conn, raw_text="b", source_channel="@c", source_message_id="2")
    items = store.list_by_state(conn, DISCOVERED)
    assert len(items) == 2


def test_check_constraint_rejects_bad_state(conn):
    item_id = store.insert_item(conn, raw_text="a", source_channel="@c", source_message_id="1")
    import sqlite3
    import pytest

    with pytest.raises(sqlite3.IntegrityError):
        store.update_state(conn, item_id, "bogus_state", from_state=DISCOVERED,
                           kind=KIND_DETERMINISTIC, actor="system")
