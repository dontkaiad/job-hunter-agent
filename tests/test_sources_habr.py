"""Tests for HabrCareerSource (job_hunter/sources.py).

Network is INJECTED (fake http_get returning canned RSS XML), so no real HTTP.
Runs against the ephemeral test PostgreSQL (the ``conn`` fixture).
Covers: item->IngestMessage mapping, fetch, watermark advance/incremental,
disabled-when-flag-off, and the registry.
"""

from __future__ import annotations

from typing import List

import pytest

from job_hunter import sources, store
from job_hunter.config import Config


# --- helpers -----------------------------------------------------------------


class FakeRssResp:
    def __init__(self, xml_text: str) -> None:
        self.text = xml_text

    def raise_for_status(self) -> None:
        pass


def _item_xml(
    vid: int,
    *,
    title: str = "LLM Engineer",
    pub: str = "Thu, 03 Jul 2026 10:00:00 +0300",
    description: str = "<p>Build <b>LLM</b> pipelines in Python.</p>",
) -> str:
    link = f"https://career.habr.com/vacancies/{vid}"
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
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>Вакансии / Хабр Карьера</title>"
        f"{body}"
        "</channel></rss>"
    )


def _cfg(**over) -> Config:
    base = dict(
        habr_enabled=True,
        habr_rss_url="https://career.habr.com/vacancies/rss?q=LLM",
        new_channel_lookback_days=14,
        database_url="",
    )
    base.update(over)
    return Config(**base)


def _habr_rows(conn) -> List[str]:
    rows = conn.execute(
        "SELECT source_message_id FROM work_items "
        "WHERE source_channel = 'habr' ORDER BY source_message_id"
    ).fetchall()
    return [r["source_message_id"] for r in rows]


# --- habr_item_to_message -----------------------------------------------------


def test_habr_item_to_message_maps_fields():
    item = {
        "title": "Senior LLM Engineer",
        "link": "https://career.habr.com/vacancies/1000133148",
        "description": "<p>Build <b>LLM</b> and <i>RAG</i> systems in Python.</p>",
        "pub_date": "Thu, 03 Jul 2026 10:00:00 +0300",
        "guid": "https://career.habr.com/vacancies/1000133148",
    }
    msg = sources.habr_item_to_message(item)
    assert msg is not None
    assert msg.source_channel == "habr"
    assert msg.source_message_id == "habr:1000133148"
    assert msg.source_link == "https://career.habr.com/vacancies/1000133148"
    assert msg.posted_at is not None and msg.posted_at.year == 2026
    assert "Senior LLM Engineer" in msg.raw_text
    assert "Хабр Карьера" in msg.raw_text
    assert "Build LLM and RAG systems in Python" in msg.raw_text
    assert "<" not in msg.raw_text  # html_to_text strips tags


def test_habr_item_to_message_missing_link_and_guid_returns_none():
    assert sources.habr_item_to_message({"title": "x", "link": "", "guid": ""}) is None


def test_habr_item_to_message_guid_fallback_to_link():
    item = {
        "title": "AI Dev",
        "link": "https://career.habr.com/vacancies/9999",
        "description": "work",
        "pub_date": None,
        "guid": None,  # guid absent -> falls back to link
    }
    msg = sources.habr_item_to_message(item)
    assert msg is not None
    assert msg.source_message_id == "habr:9999"


def test_habr_item_bad_pub_date_still_maps():
    item = {
        "title": "AI Dev",
        "link": "https://career.habr.com/vacancies/42",
        "description": "ok",
        "pub_date": "not a date",
        "guid": "https://career.habr.com/vacancies/42",
    }
    msg = sources.habr_item_to_message(item)
    assert msg is not None
    assert msg.posted_at is None


# --- fetch (injected http_get) ------------------------------------------------


def test_habr_fetch_maps_payload():
    xml = _feed_xml(_item_xml(1), _item_xml(2))
    src = sources.HabrCareerSource(http_get=lambda url: FakeRssResp(xml))
    msgs = src.fetch(_cfg())
    assert [m.source_message_id for m in msgs] == ["habr:1", "habr:2"]


def test_habr_fetch_passes_configured_url():
    seen: dict = {}

    def cap(url):
        seen["url"] = url
        return FakeRssResp(_feed_xml())

    src = sources.HabrCareerSource(http_get=cap)
    src.fetch(_cfg(habr_rss_url="https://example.com/rss?q=test"))
    assert seen["url"] == "https://example.com/rss?q=test"


# --- ingest: watermark + incremental -----------------------------------------


def test_habr_ingest_stores_items(conn):
    xml = _feed_xml(_item_xml(10), _item_xml(20))
    src = sources.HabrCareerSource(http_get=lambda url: FakeRssResp(xml))
    ids = src.ingest(_cfg(), conn)
    assert len(ids) == 2
    assert _habr_rows(conn) == ["habr:10", "habr:20"]


def test_habr_ingest_is_incremental(conn):
    xml = _feed_xml(_item_xml(30), _item_xml(31))
    src = sources.HabrCareerSource(http_get=lambda url: FakeRssResp(xml))
    cfg = _cfg()

    first = src.ingest(cfg, conn)
    assert len(first) == 2
    # identical payload second time -> watermark filters everything -> 0 new
    second = src.ingest(cfg, conn)
    assert second == []


def test_habr_ingest_disabled_when_flag_off(conn):
    xml = _feed_xml(_item_xml(50))
    src = sources.HabrCareerSource(http_get=lambda url: FakeRssResp(xml))
    result = src.ingest(_cfg(habr_enabled=False), conn)
    assert result == []
    assert _habr_rows(conn) == []


def test_habr_ingest_fetch_error_returns_empty(conn):
    def boom(url):
        raise RuntimeError("network down")

    src = sources.HabrCareerSource(http_get=boom)
    result = src.ingest(_cfg(), conn)
    assert result == []
    assert _habr_rows(conn) == []


# --- registry ----------------------------------------------------------------


def test_http_sources_registry_includes_habr():
    # off by default
    no_habr = sources.http_sources(_cfg(habr_enabled=False), mode="web")
    assert not any(s.name == "habr" for s in no_habr)
    # on when flag set
    with_habr = sources.http_sources(_cfg(habr_enabled=True), mode="web")
    assert any(s.name == "habr" for s in with_habr)


def test_http_sources_registry_habr_after_jobicy():
    cfg = _cfg(habr_enabled=True, jobicy_geos=["europe"])
    names = [s.name for s in sources.http_sources(cfg, mode="web")]
    assert names.index("habr") > names.index("jobicy")
