"""Tests for HNHiringSource (job_hunter/sources.py).

Network is INJECTED (fake http_get returning canned JSON), so no real HTTP.
Runs against the ephemeral test PostgreSQL (the ``conn`` fixture).
Covers: thread discovery, comment->IngestMessage mapping, ingest, watermark,
disabled-when-flag-off, error handling, and the registry.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from job_hunter import sources
from job_hunter.config import Config


# --- fixtures / canned data --------------------------------------------------


def _algolia_hit(object_id: int, title: str = "Ask HN: Who is hiring? (July 2026)") -> dict:
    return {"objectID": str(object_id), "title": title}


def _algolia_resp(hits: List[dict]) -> str:
    return json.dumps({"hits": hits})


def _thread(thread_id: int, kids: List[int]) -> str:
    return json.dumps({"id": thread_id, "kids": kids})


def _comment(
    cid: int,
    *,
    text: str = "<p>We're <b>hiring</b> an LLM engineer.</p>",
    time: int = 1_782_000_000,
    deleted: bool = False,
    dead: bool = False,
) -> str:
    obj: Dict[str, Any] = {"id": cid, "text": text, "time": time}
    if deleted:
        obj["deleted"] = True
    if dead:
        obj["dead"] = True
    return json.dumps(obj)


def _cfg(**over) -> Config:
    base = dict(
        hn_hiring_enabled=True,
        new_channel_lookback_days=35,
        database_url="",
    )
    base.update(over)
    return Config(**base)


def _hn_rows(conn) -> List[str]:
    rows = conn.execute(
        "SELECT source_message_id FROM work_items "
        "WHERE source_channel = 'hn_hiring' ORDER BY source_message_id"
    ).fetchall()
    return [r["source_message_id"] for r in rows]


# --- simple fake http_get ----------------------------------------------------


class FakeJsonResp:
    def __init__(self, body: str) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:
        return json.loads(self._body)


def _make_http_get(
    thread_id: int,
    kids: List[int],
    comments: Optional[Dict[int, str]] = None,
    algolia_hits: Optional[List[dict]] = None,
) -> Any:
    """Build a fake http_get that serves Algolia + Firebase canned responses."""
    if algolia_hits is None:
        algolia_hits = [_algolia_hit(thread_id)]
    comments = comments or {}

    def _get(url: str) -> FakeJsonResp:
        if "algolia" in url:
            return FakeJsonResp(_algolia_resp(algolia_hits))
        if f"item/{thread_id}.json" in url:
            return FakeJsonResp(_thread(thread_id, kids))
        # Individual comment
        for kid_id in kids:
            if f"item/{kid_id}.json" in url:
                body = comments.get(kid_id, _comment(kid_id))
                return FakeJsonResp(body)
        raise RuntimeError(f"unexpected URL: {url}")

    return _get


# --- thread discovery --------------------------------------------------------


def test_discover_thread_returns_id():
    src = sources.HNHiringSource(
        http_get=_make_http_get(12345, [])
    )
    tid = src._discover_thread()
    assert tid == 12345


def test_discover_thread_skips_non_hiring():
    hits = [
        _algolia_hit(111, "Ask HN: Who wants to be hired? (July 2026)"),
        _algolia_hit(222, "Ask HN: Who is hiring? (July 2026)"),
    ]
    src = sources.HNHiringSource(
        http_get=lambda url: FakeJsonResp(_algolia_resp(hits))
    )
    assert src._discover_thread() == 222


def test_discover_thread_returns_none_on_error():
    def boom(url):
        raise RuntimeError("network down")

    src = sources.HNHiringSource(http_get=boom)
    assert src._discover_thread() is None


def test_discover_thread_returns_none_on_empty_hits():
    src = sources.HNHiringSource(
        http_get=lambda url: FakeJsonResp(_algolia_resp([]))
    )
    assert src._discover_thread() is None


# --- fetch -------------------------------------------------------------------


def test_fetch_maps_comments():
    kids = [101, 102]
    src = sources.HNHiringSource(
        http_get=_make_http_get(1, kids)
    )
    msgs = src.fetch(1)
    assert len(msgs) == 2
    ids = {m.source_message_id for m in msgs}
    assert ids == {"hn:101", "hn:102"}


def test_fetch_sets_source_channel():
    kids = [201]
    src = sources.HNHiringSource(http_get=_make_http_get(1, kids))
    msgs = src.fetch(1)
    assert msgs[0].source_channel == "hn_hiring"


def test_fetch_sets_source_link():
    kids = [301]
    src = sources.HNHiringSource(http_get=_make_http_get(1, kids))
    msgs = src.fetch(1)
    assert msgs[0].source_link == "https://news.ycombinator.com/item?id=301"


def test_fetch_strips_html():
    kids = [401]
    comments = {401: _comment(401, text="<p>We are <b>hiring</b> at <em>Acme</em>.</p>")}
    src = sources.HNHiringSource(http_get=_make_http_get(1, kids, comments))
    msgs = src.fetch(1)
    assert "<" not in msgs[0].raw_text
    assert "hiring" in msgs[0].raw_text


def test_fetch_parses_timestamp():
    kids = [501]
    ts = 1_782_000_000
    comments = {501: _comment(501, time=ts)}
    src = sources.HNHiringSource(http_get=_make_http_get(1, kids, comments))
    msgs = src.fetch(1)
    assert msgs[0].posted_at is not None
    assert msgs[0].posted_at.year == 2026  # Unix ts 1_782_000_000 is 2026


def test_fetch_skips_deleted():
    kids = [601]
    comments = {601: _comment(601, deleted=True)}
    src = sources.HNHiringSource(http_get=_make_http_get(1, kids, comments))
    msgs = src.fetch(1)
    assert msgs == []


def test_fetch_skips_dead():
    kids = [701]
    comments = {701: _comment(701, dead=True)}
    src = sources.HNHiringSource(http_get=_make_http_get(1, kids, comments))
    msgs = src.fetch(1)
    assert msgs == []


def test_fetch_skips_empty_text():
    kids = [801]
    comments = {801: json.dumps({"id": 801, "text": "", "time": 1_782_000_000})}
    src = sources.HNHiringSource(http_get=_make_http_get(1, kids, comments))
    msgs = src.fetch(1)
    assert msgs == []


def test_fetch_tolerates_failed_comment(conn):
    """A single comment fetch error must not abort the rest."""
    call_count = {"n": 0}

    kids = [901, 902]

    def _get(url: str) -> FakeJsonResp:
        if "algolia" in url:
            return FakeJsonResp(_algolia_resp([_algolia_hit(1)]))
        if f"item/1.json" in url:
            return FakeJsonResp(_thread(1, kids))
        call_count["n"] += 1
        if "item/901.json" in url:
            raise RuntimeError("timeout")
        return FakeJsonResp(_comment(902))

    src = sources.HNHiringSource(http_get=_get)
    msgs = src.fetch(1)
    assert len(msgs) == 1
    assert msgs[0].source_message_id == "hn:902"


# --- ingest: watermark + incremental -----------------------------------------


def test_hn_ingest_stores_items(conn):
    kids = [1001, 1002]
    src = sources.HNHiringSource(http_get=_make_http_get(100, kids))
    ids = src.ingest(_cfg(), conn)
    assert len(ids) == 2
    rows = _hn_rows(conn)
    assert "hn:1001" in rows
    assert "hn:1002" in rows


def test_hn_ingest_is_incremental(conn):
    kids = [2001, 2002]
    src = sources.HNHiringSource(http_get=_make_http_get(200, kids))
    cfg = _cfg()

    first = src.ingest(cfg, conn)
    assert len(first) == 2
    second = src.ingest(cfg, conn)
    assert second == []


def test_hn_ingest_disabled_when_flag_off(conn):
    kids = [3001]
    src = sources.HNHiringSource(http_get=_make_http_get(300, kids))
    result = src.ingest(_cfg(hn_hiring_enabled=False), conn)
    assert result == []
    assert _hn_rows(conn) == []


def test_hn_ingest_no_thread_returns_empty(conn):
    src = sources.HNHiringSource(
        http_get=lambda url: FakeJsonResp(_algolia_resp([]))
    )
    result = src.ingest(_cfg(), conn)
    assert result == []


def test_hn_ingest_fetch_error_returns_empty(conn):
    def boom(url):
        raise RuntimeError("network down")

    src = sources.HNHiringSource(http_get=boom)
    result = src.ingest(_cfg(), conn)
    assert result == []
    assert _hn_rows(conn) == []


# --- registry ----------------------------------------------------------------


def test_http_sources_registry_includes_hn_hiring():
    no_hn = sources.http_sources(_cfg(hn_hiring_enabled=False), mode="web")
    assert not any(s.name == "hn_hiring" for s in no_hn)
    with_hn = sources.http_sources(_cfg(hn_hiring_enabled=True), mode="web")
    assert any(s.name == "hn_hiring" for s in with_hn)


def test_http_sources_registry_hn_after_wwr():
    cfg = _cfg(hn_hiring_enabled=True, wwr_enabled=True)
    names = [s.name for s in sources.http_sources(cfg, mode="web")]
    assert names.index("hn_hiring") > names.index("wwr")
