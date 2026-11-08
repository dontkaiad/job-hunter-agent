"""The original post's publication date is stored at ingestion (not back-filled)."""
from datetime import datetime, timedelta, timezone

import pytest

from job_hunter import ingest_telegram as it, store

WHEN = datetime(2026, 11, 3, 9, 15, tzinfo=timezone.utc)


def test_iso_utc_normalises_and_tolerates_garbage():
    assert store._iso_utc(WHEN) == "2026-11-03T09:15:00+00:00"
    assert store._iso_utc(WHEN.astimezone(timezone(timedelta(hours=3)))) == "2026-11-03T09:15:00+00:00"
    assert store._iso_utc(datetime(2026, 11, 3, 9, 15)) == "2026-11-03T09:15:00+00:00"      # naive = UTC
    assert store._iso_utc("2026-11-03T12:15:00+03:00") == "2026-11-03T09:15:00+00:00"
    for bad in (None, "", "not a date", 12345.5):
        assert store._iso_utc(bad) is None


def test_insert_item_stores_posted_at_and_defaults_to_null(conn):
    a = store.insert_item(conn, raw_text="x", source_channel="@c", source_message_id="1", posted_at=WHEN)
    b = store.insert_item(conn, raw_text="y", source_channel="@c", source_message_id="2")
    assert store.get_item(conn, a).posted_at == "2026-11-03T09:15:00+00:00"
    assert store.get_item(conn, b).posted_at is None


def test_ingestion_keeps_the_source_date(conn):
    msgs = [it.IngestMessage("@c", "10", "https://t.me/c/10", "Remote LLM engineer, python, RAG. @hr", posted_at=WHEN),
            it.IngestMessage("@c", "11", "https://t.me/c/11", "No date from this source, python engineer @hr", posted_at=None)]
    ids = it.store_messages(conn, msgs)
    got = {store.get_item(conn, i).source_message_id: store.get_item(conn, i).posted_at for i in ids}
    assert got["10"] == "2026-11-03T09:15:00+00:00" and got["11"] is None


def test_a_split_digest_gives_every_piece_the_digests_date(conn):
    text = "Вакансии недели:\n\n1. Python разработчик LLM, удалённо, 3000 EUR. @hr1\n\n2. ML инженер RAG, релокация, 4000 EUR. @hr2\n\n3. Backend Go, офис Берлин, 3500 EUR. @hr3"
    m = it.IngestMessage("@c", "20", "https://t.me/c/20", text, posted_at=WHEN)
    ids = it.store_messages(conn, [m])
    for i in ids:
        assert store.get_item(conn, i).posted_at == "2026-11-03T09:15:00+00:00"


def test_old_table_gets_the_column(pg_dsn):
    c = store.connect(pg_dsn)
    store.init_db(c)
    c.execute("ALTER TABLE work_items DROP COLUMN posted_at"); c.commit()
    store.init_db(c)
    cols = {r["column_name"] for r in c.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='work_items'").fetchall()}
    assert "posted_at" in cols
    c.close()
