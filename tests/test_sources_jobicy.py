"""Tests for the Source abstraction + JobicySource (job_hunter/sources.py).

Network is INJECTED (fake http_get returning canned Jobicy payloads), so no
real HTTP. Runs against the ephemeral test PostgreSQL (the ``conn`` fixture).
Covers: payload->IngestMessage mapping, html_to_text on jobDescription, geo
dedup, watermark advance/incremental, the registry, and the aggregator.
"""

from __future__ import annotations

from typing import List

import pytest

from job_hunter import sources, store
from job_hunter.config import Config


# --- helpers ----------------------------------------------------------------


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _job(jid, *, title="AI Engineer", geo="Europe", pub="2026-06-24T12:00:00+00:00"):
    return {
        "id": jid,
        "jobTitle": title,
        "companyName": "Acme AI",
        "jobGeo": geo,
        "jobLevel": "Senior",
        "jobType": ["Full-Time"],
        "jobExcerpt": "short stub that we must NOT use",
        "jobDescription": (
            "<p>We build <b>LLM</b> and RAG systems in <i>Python</i>.</p>"
            "<ul><li>LangChain agents</li><li>FastAPI services</li></ul>"
        ),
        "url": f"https://jobicy.com/jobs/{jid}-ai-engineer",
        "pubDate": pub,
    }


def _cfg(**over) -> Config:
    base = dict(
        jobicy_geos=["europe"],
        jobicy_industry="dev",
        jobicy_count=50,
        new_channel_lookback_days=14,
        database_url="",
    )
    base.update(over)
    return Config(**base)


def _jobicy_rows(conn) -> List[str]:
    rows = conn.execute(
        "SELECT source_message_id FROM work_items "
        "WHERE source_channel = 'jobicy' ORDER BY source_message_id"
    ).fetchall()
    return [r["source_message_id"] for r in rows]


# --- payload -> IngestMessage ----------------------------------------------


def test_job_to_message_maps_contract_fields():
    msg = sources.job_to_message(_job(123), "europe")
    assert msg is not None
    assert msg.source_channel == "jobicy"          # constant -> cross-geo dedup
    assert msg.source_message_id == "jobicy:123"    # stable id
    assert msg.source_link == "https://jobicy.com/jobs/123-ai-engineer"
    assert msg.posted_at is not None and msg.posted_at.year == 2026
    # raw_text = title + company + meta + FULL stripped description (+ url).
    assert "AI Engineer" in msg.raw_text
    assert "Company: Acme AI" in msg.raw_text
    assert "Remote" in msg.raw_text and "Senior" in msg.raw_text
    # html_to_text applied: tags gone, text kept.
    assert "We build LLM and RAG systems in Python" in msg.raw_text
    assert "LangChain agents" in msg.raw_text
    assert "<" not in msg.raw_text  # no HTML survives
    # the 219-char excerpt must NOT be used as the body.
    assert "short stub" not in msg.raw_text


def test_job_to_message_missing_id_returns_none():
    assert sources.job_to_message({"jobTitle": "x"}, "europe") is None


# --- fetch (injected http_get) ---------------------------------------------


def test_fetch_maps_payload():
    src = sources.JobicySource(
        http_get=lambda url: FakeResp({"jobs": [_job(1), _job(2)]}),
        request_delay=0,
    )
    msgs = src.fetch(_cfg(), "europe")
    assert [m.source_message_id for m in msgs] == ["jobicy:1", "jobicy:2"]


def test_fetch_url_carries_geo_industry_count():
    seen = {}

    def cap(url):
        seen["url"] = url
        return FakeResp({"jobs": []})

    sources.JobicySource(http_get=cap, request_delay=0).fetch(
        _cfg(jobicy_industry="dev", jobicy_count=50), "poland"
    )
    assert "geo=poland" in seen["url"]
    assert "industry=dev" in seen["url"]
    assert "count=50" in seen["url"]


# --- ingest: geo dedup + watermark -----------------------------------------


def test_ingest_dedups_same_job_across_geos(conn):
    # job 100 appears under BOTH europe and germany; each geo also has a unique one.
    def fake_get(url):
        if "geo=europe" in url:
            return FakeResp({"jobs": [_job(100), _job(101)]})
        if "geo=germany" in url:
            return FakeResp({"jobs": [_job(100), _job(102)]})
        return FakeResp({"jobs": []})

    cfg = _cfg(jobicy_geos=["europe", "germany"])
    src = sources.JobicySource(http_get=fake_get, request_delay=0)
    ids = src.ingest(cfg, conn)

    # 100 inserted once (europe), deduped on germany -> 3 distinct rows total.
    assert len(ids) == 3
    assert _jobicy_rows(conn) == ["jobicy:100", "jobicy:101", "jobicy:102"]
    # per-geo cursors both advanced (independent watermarks).
    assert store.get_channel_watermark(conn, "jobicy:europe") is not None
    assert store.get_channel_watermark(conn, "jobicy:germany") is not None


def test_ingest_is_incremental_via_watermark(conn):
    payload = {"jobs": [_job(200), _job(201)]}
    src = sources.JobicySource(http_get=lambda url: FakeResp(payload), request_delay=0)
    cfg = _cfg(jobicy_geos=["europe"])

    first = src.ingest(cfg, conn)
    assert len(first) == 2
    # Second run, identical payload -> watermark filters everything -> 0 new.
    second = src.ingest(cfg, conn)
    assert second == []


def test_ingest_disabled_when_no_geos(conn):
    src = sources.JobicySource(
        http_get=lambda url: FakeResp({"jobs": [_job(1)]}), request_delay=0
    )
    assert src.ingest(_cfg(jobicy_geos=[]), conn) == []
    assert _jobicy_rows(conn) == []


def test_ingest_single_geo_one_item_not_split(conn):
    # A normal single-vacancy description must map to exactly ONE item (the
    # shared store_messages digest-splitter must not chop a normal job post).
    src = sources.JobicySource(
        http_get=lambda url: FakeResp({"jobs": [_job(300)]}), request_delay=0
    )
    ids = src.ingest(_cfg(jobicy_geos=["europe"]), conn)
    assert len(ids) == 1
    item = store.get_item(conn, ids[0])
    assert item.source_channel == "jobicy"
    assert "LangChain agents" in item.raw_text


def test_ingest_bad_geo_does_not_abort_sweep(conn):
    # europe raises; germany must still ingest.
    def fake_get(url):
        if "geo=europe" in url:
            raise RuntimeError("boom")
        return FakeResp({"jobs": [_job(400)]})

    cfg = _cfg(jobicy_geos=["europe", "germany"])
    ids = sources.JobicySource(http_get=fake_get, request_delay=0).ingest(cfg, conn)
    assert _jobicy_rows(conn) == ["jobicy:400"]
    assert len(ids) == 1


# --- registry + aggregator --------------------------------------------------


def test_http_sources_registry():
    assert [s.name for s in sources.http_sources(_cfg(jobicy_geos=[]), mode="web")] == [
        "telegram-web"
    ]
    assert [
        s.name for s in sources.http_sources(_cfg(jobicy_geos=["europe"]), mode="web")
    ] == ["telegram-web", "jobicy"]
    # telethon path handles telegram itself -> only jobicy is an http source.
    assert [
        s.name
        for s in sources.http_sources(_cfg(jobicy_geos=["europe"]), mode="telethon")
    ] == ["jobicy"]


class _FakeSource:
    name = "fake"

    def __init__(self, ids):
        self._ids = ids

    def ingest(self, cfg, conn):
        return list(self._ids)

    def ingest_with_own_connection(self, cfg):
        return list(self._ids)


def test_aggregate_concatenates_telegram_and_jobicy(conn):
    jobicy = sources.JobicySource(
        http_get=lambda url: FakeResp({"jobs": [_job(500), _job(501)]}),
        request_delay=0,
    )
    cfg = _cfg(jobicy_geos=["europe"])
    # FakeSource stands in for the telegram-web source; jobicy is real.
    out = sources.aggregate(cfg, conn, [_FakeSource([9001]), jobicy])
    assert out[0] == 9001
    assert len(out) == 3  # 1 telegram + 2 jobicy
    assert _jobicy_rows(conn) == ["jobicy:500", "jobicy:501"]
