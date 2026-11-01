"""Shadow sample: a deterministic share of below-the-cut deletions is kept in eval_shadow."""
import json

import pytest

from job_hunter import pipeline, store
from job_hunter.pipeline import Deps

POST = ("Remote Senior Python engineer building LLM RAG agents with Claude API and "
        "FastAPI. Salary 250000-350000 RUB. Contact @hr_acme")


def _deps(client, fx, **kw):
    return Deps(llm_client=client, fx=fx, use_llm_extract=False, min_persist_score=25, **kw)


def _shadow_rows(conn):
    return conn.execute("select * from eval_shadow order by id").fetchall()


# --- selection --------------------------------------------------------------

def test_selection_is_deterministic_and_about_the_configured_rate():
    ids = range(1, 20001)
    first = [store.shadow_selected(i, 10) for i in ids]
    assert first == [store.shadow_selected(i, 10) for i in ids]          # same answer every time
    share = sum(first) / len(first)
    assert 0.09 < share < 0.11
    assert not any(store.shadow_selected(i, 0) for i in range(500))      # 0 disables
    assert all(store.shadow_selected(i, 100) for i in range(500))


# --- copy through the real pipeline ----------------------------------------

def test_model_scored_deletion_is_copied_and_work_items_row_is_gone(conn, fake_llm, fake_fx):
    deps = _deps(fake_llm, fake_fx, shadow_rate_percent=100)
    iid = store.insert_item(conn, raw_text=POST, source_channel="@c", source_message_id="1")
    fake_llm.responses = ['{"relevance_score": 12, "Обоснование": "wrong domain"}']
    pipeline.run_to_gate(conn, iid, deps=deps)
    assert store.get_item(conn, iid) is None                              # still deleted
    rows = _shadow_rows(conn)
    assert len(rows) == 1 and rows[0]["orig_item_id"] == iid
    assert rows[0]["score"] == 12.0 and rows[0]["raw_text"] == POST
    assert "title" in json.loads(rows[0]["extracted_json"])


def test_not_copied_when_rate_is_zero_or_score_is_kept(conn, fake_llm, fake_fx):
    off = _deps(fake_llm, fake_fx, shadow_rate_percent=0)
    iid = store.insert_item(conn, raw_text=POST, source_channel="@c", source_message_id="1")
    fake_llm.responses = ['{"relevance_score": 12, "Обоснование": "x"}']
    pipeline.run_to_gate(conn, iid, deps=off)
    on = _deps(fake_llm, fake_fx, shadow_rate_percent=100)
    kept = store.insert_item(conn, raw_text=POST + " v2", source_channel="@c", source_message_id="2")
    fake_llm.responses = ['{"relevance_score": 40, "Обоснование": "x"}']
    pipeline.run_to_gate(conn, kept, deps=on)
    assert _shadow_rows(conn) == [] and store.get_item(conn, kept) is not None


def test_prefilter_drops_are_not_sampled(conn, fake_llm, fake_fx):
    deps = _deps(fake_llm, fake_fx, shadow_rate_percent=100)
    junk = store.insert_item(conn, raw_text="ищу работу python разработчик, резюме в личке всем привет",
                             source_channel="@c", source_message_id="1")
    pipeline.run_to_gate(conn, junk, deps=deps)
    assert store.get_item(conn, junk) is None and _shadow_rows(conn) == []


def test_row_cap_keeps_the_earliest_and_stops_adding(conn, fake_llm, fake_fx):
    deps = _deps(fake_llm, fake_fx, shadow_rate_percent=100, shadow_max_rows=3)
    for n in range(1, 6):
        iid = store.insert_item(conn, raw_text=POST + f" #{n}", source_channel="@c", source_message_id=str(n))
        fake_llm.responses = ['{"relevance_score": 10, "Обоснование": "x"}']
        pipeline.run_to_gate(conn, iid, deps=deps)
        assert store.get_item(conn, iid) is None                          # every one still deleted
    assert len(_shadow_rows(conn)) == 3


def test_old_rows_expire(conn, fake_llm, fake_fx):
    conn.execute("INSERT INTO eval_shadow (orig_item_id, score, created_at) VALUES (999, 5, '2020-01-01T00:00:00+00:00')")
    conn.commit()
    deps = _deps(fake_llm, fake_fx, shadow_rate_percent=100, shadow_ttl_days=90)
    iid = store.insert_item(conn, raw_text=POST, source_channel="@c", source_message_id="1")
    fake_llm.responses = ['{"relevance_score": 10, "Обоснование": "x"}']
    pipeline.run_to_gate(conn, iid, deps=deps)
    ids = [r["orig_item_id"] for r in _shadow_rows(conn)]
    assert 999 not in ids and iid in ids


def test_a_failing_copy_never_blocks_the_deletion(conn, fake_llm, fake_fx, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(store, "shadow_copy", boom)
    deps = _deps(fake_llm, fake_fx, shadow_rate_percent=100)
    iid = store.insert_item(conn, raw_text=POST, source_channel="@c", source_message_id="1")
    fake_llm.responses = ['{"relevance_score": 10, "Обоснование": "x"}']
    pipeline.run_to_gate(conn, iid, deps=deps)
    assert store.get_item(conn, iid) is None


def test_shadow_rows_are_invisible_to_the_pipeline(conn, fake_llm, fake_fx):
    deps = _deps(fake_llm, fake_fx, shadow_rate_percent=100)
    iid = store.insert_item(conn, raw_text=POST, source_channel="@c", source_message_id="1")
    fake_llm.responses = ['{"relevance_score": 10, "Обоснование": "x"}']
    pipeline.run_to_gate(conn, iid, deps=deps)
    assert conn.execute("select count(*) n from work_items").fetchone()["n"] == 0
    # a re-post of the same vacancy is a fresh item: shadow rows never feed dedup
    again = store.insert_item(conn, raw_text=POST, source_channel="@d", source_message_id="9")
    assert store.find_duplicate_by_url_keys(conn, ["https://x.io/a"], again) is None


def test_config_defaults_and_deps_wiring():
    from job_hunter.config import Config
    c = Config()
    assert (c.shadow_rate_percent, c.shadow_max_rows, c.shadow_ttl_days) == (10, 200, 90)
    assert Deps().shadow_rate_percent == 0       # off unless configured (tests, scripts)
