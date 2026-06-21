"""Ingestion date-awareness (Part 4): pure date parser, incremental filter,
per-channel watermark, and the new-channel 14-day window. No real network.
"""

from datetime import datetime, timedelta, timezone

from job_hunter import ingest_web as web
from job_hunter import store
from job_hunter.config import Config
from job_hunter.ingest_telegram import IngestMessage


# --- Pure date parser -------------------------------------------------------


def test_parse_post_datetime_with_offset():
    dt = web.parse_post_datetime("2026-06-20T10:00:00+00:00")
    assert dt == datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)
    assert dt.tzinfo is not None  # tz-aware


def test_parse_post_datetime_z_suffix():
    dt = web.parse_post_datetime("2026-06-20T10:00:00Z")
    assert dt == datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)


def test_parse_post_datetime_naive_assumed_utc():
    dt = web.parse_post_datetime("2026-06-20T10:00:00")
    assert dt.tzinfo == timezone.utc


def test_parse_post_datetime_bad_value():
    assert web.parse_post_datetime("not-a-date") is None
    assert web.parse_post_datetime(None) is None
    assert web.parse_post_datetime("") is None


SAMPLE_HTML = """
<section class="tgme_channel_history js-message_history">
  <div class="tgme_widget_message js-widget_message" data-post="jobschan/101">
    <div class="tgme_widget_message_text js-message_text">
      Senior Python Engineer (Remote). Stack: Python, FastAPI.
    </div>
    <div class="tgme_widget_message_footer">
      <a class="tgme_widget_message_date" href="https://t.me/jobschan/101">
        <time datetime="2026-06-20T10:00:00+00:00">10:00</time>
      </a>
    </div>
  </div>
  <div class="tgme_widget_message js-widget_message" data-post="jobschan/102">
    <div class="tgme_widget_message_text js-message_text">
      Middle LLM role. Salary 250000 RUB. Remote OK.
    </div>
    <a class="tgme_widget_message_date" href="https://t.me/jobschan/102">
      <time datetime="2026-06-21T11:30:00+00:00">11:30</time>
    </a>
  </div>
</section>
"""


def test_parse_channel_html_captures_post_datetimes():
    msgs = web.parse_channel_html(SAMPLE_HTML, "@jobschan")
    assert [m.source_message_id for m in msgs] == ["101", "102"]
    assert msgs[0].posted_at == datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)
    assert msgs[1].posted_at == datetime(2026, 6, 21, 11, 30, tzinfo=timezone.utc)


# --- Cutoff + filter (pure) -------------------------------------------------


def test_ingestion_cutoff_new_channel_uses_lookback():
    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    cutoff = web.ingestion_cutoff(None, now, lookback_days=14)
    assert cutoff == now - timedelta(days=14)


def test_ingestion_cutoff_existing_channel_uses_watermark():
    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    wm = datetime(2026, 6, 19, 9, 0, tzinfo=timezone.utc)
    assert web.ingestion_cutoff(wm, now, lookback_days=14) == wm


def _msg(mid, dt):
    return IngestMessage("@c", str(mid), f"https://t.me/c/{mid}", "post text here",
                         posted_at=dt)


def test_filter_new_messages_keeps_only_newer():
    cutoff = datetime(2026, 6, 20, 0, 0, tzinfo=timezone.utc)
    msgs = [
        _msg(1, datetime(2026, 6, 19, tzinfo=timezone.utc)),  # old -> drop
        _msg(2, datetime(2026, 6, 21, tzinfo=timezone.utc)),  # new -> keep
        _msg(3, None),                                        # unknown -> keep
    ]
    kept = web.filter_new_messages(msgs, cutoff)
    assert [m.source_message_id for m in kept] == ["2", "3"]


# --- Watermark store roundtrip ---------------------------------------------


def test_channel_watermark_roundtrip(conn):
    assert store.get_channel_watermark(conn, "@c") is None  # new channel
    dt = datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)
    store.set_channel_watermark(conn, "@c", dt)
    got = store.get_channel_watermark(conn, "@c")
    assert got == dt
    assert got.tzinfo is not None


def test_channel_watermark_never_goes_backwards(conn):
    later = datetime(2026, 6, 21, tzinfo=timezone.utc)
    earlier = datetime(2026, 6, 19, tzinfo=timezone.utc)
    store.set_channel_watermark(conn, "@c", later)
    store.set_channel_watermark(conn, "@c", earlier)  # must NOT regress
    assert store.get_channel_watermark(conn, "@c") == later


# --- End-to-end incremental ingest -----------------------------------------


class FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def test_new_channel_ingests_within_14_day_window(conn, monkeypatch):
    # Freeze "now" so the SAMPLE_HTML posts (2026-06-20/21) are inside the window.
    fixed_now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(web, "now_utc", lambda: fixed_now)

    cfg = Config(telegram_channels=["@jobschan"], new_channel_lookback_days=14)
    reader = web.WebReader(http_get=lambda url: FakeResp(SAMPLE_HTML))
    ids = web.ingest(cfg, conn, reader=reader)
    assert len(ids) == 2  # both within 14 days
    # Watermark advanced to the newest post (102 @ 11:30 on the 21st).
    wm = store.get_channel_watermark(conn, "@jobschan")
    assert wm == datetime(2026, 6, 21, 11, 30, tzinfo=timezone.utc)


def test_new_channel_drops_posts_older_than_window(conn, monkeypatch):
    # Move "now" far ahead so the SAMPLE_HTML posts are older than 14 days.
    fixed_now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(web, "now_utc", lambda: fixed_now)

    cfg = Config(telegram_channels=["@jobschan"], new_channel_lookback_days=14)
    reader = web.WebReader(http_get=lambda url: FakeResp(SAMPLE_HTML))
    ids = web.ingest(cfg, conn, reader=reader)
    assert ids == []  # all posts older than the 14-day window


def test_incremental_only_takes_posts_newer_than_watermark(conn, monkeypatch):
    fixed_now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(web, "now_utc", lambda: fixed_now)

    # Pre-set a watermark between the two posts: 101 (06-20) is old, 102 (06-21) new.
    store.set_channel_watermark(
        conn, "@jobschan", datetime(2026, 6, 20, 23, 0, tzinfo=timezone.utc)
    )
    cfg = Config(telegram_channels=["@jobschan"], new_channel_lookback_days=14)
    reader = web.WebReader(http_get=lambda url: FakeResp(SAMPLE_HTML))
    ids = web.ingest(cfg, conn, reader=reader)
    assert len(ids) == 1  # only the post newer than the watermark (102)
    items = store.list_by_state(conn, "discovered")
    assert [i.source_message_id for i in items] == ["102"]


def test_second_run_ingests_nothing_new(conn, monkeypatch):
    fixed_now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(web, "now_utc", lambda: fixed_now)

    cfg = Config(telegram_channels=["@jobschan"], new_channel_lookback_days=14)
    reader = web.WebReader(http_get=lambda url: FakeResp(SAMPLE_HTML))
    assert len(web.ingest(cfg, conn, reader=reader)) == 2
    # Watermark now at the newest post; a second run sees nothing newer.
    assert web.ingest(cfg, conn, reader=reader) == []
