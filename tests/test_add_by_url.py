"""Tests for the shared add-by-URL flow (job_hunter/add_by_url.py).

Runs against the ephemeral test PostgreSQL (the ``conn`` fixture). The page
fetch is INJECTED (fake), so no network. The pipeline (extract->score->advance)
runs for real through the ``deps`` fixture's fake LLM, so the resulting state +
score are produced by the SAME code a harvested card uses.
"""

from __future__ import annotations

import pytest

from job_hunter import add_by_url, store
from job_hunter.states import SURFACED

GOOD_TEXT = (
    "Senior Python LLM RAG Engineer. Remote. We build retrieval-augmented "
    "generation systems with Claude and FastAPI. Salary 300000-400000 RUB. "
    "Contact @hr to apply. " * 4
)


def _fetch_ok(_url):
    return GOOD_TEXT


def _fetch_empty(_url):
    return None


# --- pure helpers -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://Career.Example.com/jobs/42/", "career.example.com/jobs/42"),
        ("http://example.com/a/?vacancyId=7#frag", "example.com/a?vacancyId=7"),
        ("https://t.me/jobs/123", "t.me/jobs/123"),
        ("not a url", None),
        ("ftp://example.com/x", None),
        ("", None),
    ],
)
def test_manual_message_id(raw, expected):
    assert add_by_url.manual_message_id(raw) == expected


def test_message_id_scheme_independent():
    # http:// and https:// of the same page dedup to ONE key.
    assert add_by_url.manual_message_id(
        "http://example.com/jobs/1"
    ) == add_by_url.manual_message_id("https://example.com/jobs/1")


# --- the flow ---------------------------------------------------------------


def test_added_runs_pipeline_and_surfaces(conn, deps):
    url = "https://career.example.com/jobs/42"
    out = add_by_url.add_by_url(conn, url, deps, fetch=_fetch_ok)

    assert out.status == "added"
    assert out.state == SURFACED  # deps fixture's judge returns 85 -> surfaced
    assert out.score == 85

    item = store.get_item(conn, out.item_id)
    assert item.source_channel == add_by_url.MANUAL_SOURCE
    assert item.source_link == url
    # raw_text carries the URL line + the fetched body.
    assert url in item.raw_text
    assert "retrieval-augmented" in item.raw_text


def test_duplicate_url_no_second_card(conn, deps):
    url = "https://career.example.com/jobs/99"
    first = add_by_url.add_by_url(conn, url, deps, fetch=_fetch_ok)
    assert first.status == "added"

    # Same URL with a trailing slash + fragment normalizes to the same key.
    second = add_by_url.add_by_url(conn, url + "/#apply", deps, fetch=_fetch_ok)
    assert second.status == "duplicate"
    assert second.item_id == first.item_id

    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM work_items WHERE source_channel = %s",
        (add_by_url.MANUAL_SOURCE,),
    ).fetchone()
    assert rows["n"] == 1


def test_unreadable_inserts_nothing(conn, deps):
    out = add_by_url.add_by_url(
        conn, "https://example.com/js-only", deps, fetch=_fetch_empty
    )
    assert out.status == "unreadable"
    assert out.item_id is None
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM work_items WHERE source_channel = %s",
        (add_by_url.MANUAL_SOURCE,),
    ).fetchone()["n"]
    assert n == 0


def test_invalid_url_never_fetches(conn, deps):
    calls = {"n": 0}

    def _spy(_url):
        calls["n"] += 1
        return GOOD_TEXT

    out = add_by_url.add_by_url(conn, "not-a-url", deps, fetch=_spy)
    assert out.status == "invalid_url"
    assert calls["n"] == 0  # bailed before any fetch


def test_telegram_url_is_just_another_source(conn, deps):
    # A pasted t.me link is NOT special-cased here: if the page yields text it is
    # added like any other source.
    out = add_by_url.add_by_url(
        conn, "https://t.me/s/somechannel", deps, fetch=_fetch_ok
    )
    assert out.status == "added"
    assert store.get_item(conn, out.item_id).source_channel == "manual"
