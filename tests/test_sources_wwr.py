"""Tests for WeworkRemotelySource (job_hunter/sources.py).

Network is INJECTED (fake http_get returning canned RSS XML), so no real HTTP.
Runs against the ephemeral test PostgreSQL (the ``conn`` fixture).
Covers: item->IngestMessage mapping, fetch, per-feed watermark, incremental,
disabled-when-flag-off, and the registry.
"""

from __future__ import annotations

from typing import List

import pytest

from job_hunter import sources
from job_hunter.config import Config


# --- helpers -----------------------------------------------------------------


class FakeRssResp:
    def __init__(self, xml_text: str) -> None:
        self.text = xml_text

    def raise_for_status(self) -> None:
        pass


def _item_xml(
    slug: str,
    *,
    title: str = "Senior LLM Engineer",
    pub: str = "Thu, 03 Jul 2026 10:00:00 +0000",
    description: str = "<p>Build <b>RAG</b> systems in Python.</p>",
) -> str:
    link = f"https://weworkremotely.com/remote-jobs/openings/{slug}"
    return (
        f"<item>"
        f"<title>{title}</title>"
        f"<link>{link}</link>"
        f"<description>{description}</description>"
        f"<pubDate>{pub}</pubDate>"
        f"<guid>{link}</guid>"
        f"</item>"
    )


def _feed_xml(*items: str) -> str:
    body = "".join(items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'


def _cfg(**over) -> Config:
    base = dict(
        wwr_enabled=True,
        wwr_rss_urls=[
            "https://weworkremotely.com/categories/remote-programming-jobs.rss",
            "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
        ],
        new_channel_lookback_days=14,
        database_url="",
    )
    base.update(over)
    return Config(**base)


def _wwr_rows(conn) -> List[str]:
    rows = conn.execute(
        "SELECT source_message_id FROM work_items "
        "WHERE source_channel = 'wwr' ORDER BY source_message_id"
    ).fetchall()
    return [r["source_message_id"] for r in rows]


# --- wwr_item_to_message -----------------------------------------------------


def test_wwr_item_to_message_maps_fields():
    item = {
        "title": "Senior LLM Engineer",
        "link": "https://weworkremotely.com/remote-jobs/openings/abc123-llm-eng",
        "description": "<p>Build <b>RAG</b> pipelines.</p>",
        "pub_date": "Thu, 03 Jul 2026 10:00:00 +0000",
        "guid": "https://weworkremotely.com/remote-jobs/openings/abc123-llm-eng",
    }
    msg = sources.wwr_item_to_message(item)
    assert msg is not None
    assert msg.source_channel == "wwr"
    assert msg.source_message_id == "wwr:abc123-llm-eng"
    assert msg.source_link == "https://weworkremotely.com/remote-jobs/openings/abc123-llm-eng"
    assert msg.posted_at is not None and msg.posted_at.year == 2026
    assert "Senior LLM Engineer" in msg.raw_text
    assert "We Work Remotely" in msg.raw_text
    assert "Build RAG pipelines" in msg.raw_text
    assert "<" not in msg.raw_text


def test_wwr_item_to_message_missing_link_returns_none():
    assert sources.wwr_item_to_message({"title": "x", "link": "", "guid": ""}) is None


def test_wwr_item_to_message_guid_fallback_to_link():
    item = {
        "title": "AI Dev",
        "link": "https://weworkremotely.com/remote-jobs/openings/xyz-ai-dev",
        "description": "work",
        "pub_date": None,
        "guid": None,
    }
    msg = sources.wwr_item_to_message(item)
    assert msg is not None
    assert msg.source_message_id == "wwr:xyz-ai-dev"


def test_wwr_item_bad_pub_date_still_maps():
    item = {
        "title": "AI Dev",
        "link": "https://weworkremotely.com/remote-jobs/openings/slug-42",
        "description": "ok",
        "pub_date": "not a date",
        "guid": "https://weworkremotely.com/remote-jobs/openings/slug-42",
    }
    msg = sources.wwr_item_to_message(item)
    assert msg is not None
    assert msg.posted_at is None


# --- fetch (injected http_get) -----------------------------------------------


def test_wwr_fetch_maps_payload():
    xml = _feed_xml(_item_xml("job-1"), _item_xml("job-2"))
    src = sources.WeworkRemotelySource(
        http_get=lambda url: FakeRssResp(xml)
    )
    msgs = src.fetch("https://weworkremotely.com/categories/remote-programming-jobs.rss")
    assert [m.source_message_id for m in msgs] == ["wwr:job-1", "wwr:job-2"]


def test_wwr_fetch_passes_url():
    seen: dict = {}

    def cap(url):
        seen["url"] = url
        return FakeRssResp(_feed_xml())

    src = sources.WeworkRemotelySource(http_get=cap)
    src.fetch("https://example.com/custom.rss")
    assert seen["url"] == "https://example.com/custom.rss"


# --- ingest: watermark + incremental -----------------------------------------


def test_wwr_ingest_stores_items(conn):
    xml1 = _feed_xml(_item_xml("prog-10"), _item_xml("prog-20"))
    xml2 = _feed_xml(_item_xml("full-30"))

    feeds = {
        "https://weworkremotely.com/categories/remote-programming-jobs.rss": xml1,
        "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss": xml2,
    }
    src = sources.WeworkRemotelySource(http_get=lambda url: FakeRssResp(feeds[url]))
    ids = src.ingest(_cfg(), conn)
    assert len(ids) == 3
    rows = _wwr_rows(conn)
    assert "wwr:prog-10" in rows
    assert "wwr:prog-20" in rows
    assert "wwr:full-30" in rows


def test_wwr_ingest_is_incremental(conn):
    xml = _feed_xml(_item_xml("prog-50"), _item_xml("prog-51"))
    feeds = {
        "https://weworkremotely.com/categories/remote-programming-jobs.rss": xml,
        "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss": _feed_xml(),
    }
    src = sources.WeworkRemotelySource(http_get=lambda url: FakeRssResp(feeds[url]))
    cfg = _cfg()

    first = src.ingest(cfg, conn)
    assert len(first) == 2
    second = src.ingest(cfg, conn)
    assert second == []


def test_wwr_ingest_disabled_when_flag_off(conn):
    xml = _feed_xml(_item_xml("prog-99"))
    src = sources.WeworkRemotelySource(http_get=lambda url: FakeRssResp(xml))
    result = src.ingest(_cfg(wwr_enabled=False), conn)
    assert result == []
    assert _wwr_rows(conn) == []


def test_wwr_ingest_fetch_error_returns_empty(conn):
    def boom(url):
        raise RuntimeError("network down")

    src = sources.WeworkRemotelySource(http_get=boom)
    result = src.ingest(_cfg(), conn)
    assert result == []
    assert _wwr_rows(conn) == []


def test_wwr_dedup_across_feeds(conn):
    # Same slug in both feeds -> only one row inserted.
    xml_shared = _feed_xml(_item_xml("shared-slug"))
    feeds = {
        "https://weworkremotely.com/categories/remote-programming-jobs.rss": xml_shared,
        "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss": xml_shared,
    }
    src = sources.WeworkRemotelySource(http_get=lambda url: FakeRssResp(feeds[url]))
    src.ingest(_cfg(), conn)
    assert _wwr_rows(conn).count("wwr:shared-slug") == 1


# --- registry ----------------------------------------------------------------


def test_http_sources_registry_includes_wwr():
    no_wwr = sources.http_sources(_cfg(wwr_enabled=False), mode="web")
    assert not any(s.name == "wwr" for s in no_wwr)
    with_wwr = sources.http_sources(_cfg(wwr_enabled=True), mode="web")
    assert any(s.name == "wwr" for s in with_wwr)


def test_http_sources_registry_wwr_after_habr():
    cfg = _cfg(wwr_enabled=True, habr_enabled=True)
    names = [s.name for s in sources.http_sources(cfg, mode="web")]
    assert names.index("wwr") > names.index("habr")
