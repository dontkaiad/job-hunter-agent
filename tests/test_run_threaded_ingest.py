"""Regression test for the sqlite cross-thread bug in the web ingest path.

Smoke testing surfaced:

    sqlite3.ProgrammingError: SQLite objects created in a thread can only be
    used in that same thread.

Root cause: run.ingest used to do
``asyncio.to_thread(ingest_web.ingest, cfg, conn)`` -- the connection was
created in the main thread but store.insert_item ran in the worker thread,
violating sqlite3 thread affinity.

This test exercises the REAL threaded path:
  - goes through run.ingest -> asyncio.to_thread (real threading, NOT mocked),
  - writes to a REAL file-based sqlite DB under tmp_path (so the connection
    genuinely owns/crosses threads exactly as in production),
  - mocks ONLY the HTTP fetch (no network) -- the sqlite layer is real.

Without the fix (passing the main-thread connection into the worker thread)
this test reproduces the ProgrammingError. With the fix (the worker opens its
own connection) it passes, and the main-thread connection still observes the
rows the worker committed.
"""

from __future__ import annotations

import asyncio

from job_hunter import ingest_web as web
from job_hunter import run, store
from job_hunter.config import Config

# Two real message blocks; mirrors the captured t.me/s/ markup.
SAMPLE_HTML = """
<section class="tgme_channel_history js-message_history">
  <div class="tgme_widget_message js-widget_message" data-post="jobschan/501">
    <div class="tgme_widget_message_text js-message_text">
      Senior Python Engineer (Remote). Stack: Python, FastAPI.
    </div>
  </div>
  <div class="tgme_widget_message js-widget_message" data-post="jobschan/502">
    <div class="tgme_widget_message_text js-message_text">
      Middle LLM role. Salary 250000 RUB. Remote OK.
    </div>
  </div>
</section>
"""


class FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def _fake_get(url):
    # No network: every channel returns the same captured HTML.
    return FakeResp(SAMPLE_HTML)


def test_run_ingest_web_crosses_thread_with_real_sqlite(tmp_path, monkeypatch):
    """run.ingest (web mode) must work end-to-end through asyncio.to_thread
    against a real file-based sqlite DB. Reproduces the thread-affinity bug
    without the fix; passes with it.
    """
    db_path = str(tmp_path / "thread_affinity.db")
    cfg = Config(ingest_mode="web", telegram_channels=["@jobschan"], db_path=db_path)

    # Force the REAL httpx fetch to use our fake (no network). We patch the
    # module-level default so WebReader() built inside the worker thread uses
    # it -- the threading and the sqlite layer remain entirely real.
    monkeypatch.setattr(web, "_default_get", _fake_get)

    # Mirror run._amain: the MAIN thread opens its connection on the same file
    # DB, then the web ingest is offloaded to a worker thread.
    conn = store.connect(cfg.db_path)
    store.init_db(conn)
    try:
        # This goes through asyncio.to_thread -> ingest_with_own_connection,
        # which opens/uses/closes its own connection in the worker thread.
        new_ids = asyncio.run(run.ingest(cfg, conn))
        assert len(new_ids) == 2

        # The main-thread connection must observe the rows the worker thread
        # committed (proves the cross-thread file DB write is visible here).
        discovered = store.list_by_state(conn, "discovered")
        assert len(discovered) == 2
        assert {i.source_message_id for i in discovered} == {"501", "502"}

        # Dedup still holds across a second threaded run.
        new_ids2 = asyncio.run(run.ingest(cfg, conn))
        assert new_ids2 == []
        assert len(store.list_by_state(conn, "discovered")) == 2

        # Datetimes written by the worker are timezone-aware (offset present).
        created = discovered[0].created_at
        assert ("+" in created) or created.endswith("Z")
    finally:
        conn.close()


def test_ingest_with_own_connection_writes_real_db(tmp_path):
    """The worker unit-of-work opens its own connection and persists rows that
    survive after it closes -- verified by reopening the file DB."""
    db_path = str(tmp_path / "own_conn.db")
    cfg = Config(ingest_mode="web", telegram_channels=["@jobschan"], db_path=db_path)
    reader = web.WebReader(http_get=_fake_get)

    new_ids = web.ingest_with_own_connection(cfg, reader=reader)
    assert len(new_ids) == 2

    # Reopen the file DB with a brand-new connection: rows were committed.
    conn2 = store.connect(db_path)
    try:
        assert len(store.list_by_state(conn2, "discovered")) == 2
    finally:
        conn2.close()
