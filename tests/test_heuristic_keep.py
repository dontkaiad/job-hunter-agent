"""Heuristic-extract vacancies are never deleted by a low score: kept 30 days, shadowed."""
from datetime import datetime, timedelta, timezone

import pytest

from job_hunter import pipeline, run, store
from job_hunter.pipeline import Deps

POST = ("Remote Senior Python engineer building LLM RAG agents with Claude API and "
        "FastAPI. Salary 250000-350000 RUB. Contact @hr_acme")
LOW = '{"relevance_score": 10, "Обоснование": "wrong domain"}'


def _deps(fake_llm, fx, **kw):
    return Deps(llm_client=fake_llm, fx=fx, use_llm_extract=False, min_persist_score=25, **kw)


def _scored_low(conn, fake_llm, fx, mode, mid="1", **kw):
    """Run one vacancy with a given extract_mode to the score gate with a model score of 10."""
    deps = _deps(fake_llm, fx, **kw)
    iid = store.insert_item(conn, raw_text=POST + f" #{mid}", source_channel="@c", source_message_id=mid)
    pipeline.advance_by_id(conn, iid, deps=deps)                         # extract (heuristic in tests)
    conn.execute("UPDATE work_items SET extract_mode=%s WHERE id=%s", (mode, iid)); conn.commit()
    fake_llm.responses = [LOW]
    pipeline.run_to_gate(conn, iid, deps=deps)
    return iid, deps


@pytest.mark.parametrize("mode", ["heuristic:topic", "heuristic:fallback"])
def test_protected_modes_are_kept_not_deleted(conn, fake_llm, fake_fx, mode):
    iid, deps = _scored_low(conn, fake_llm, fake_fx, mode)
    it = store.get_item(conn, iid)
    assert it is not None and it.state == "rejected" and it.relevance_score == 10.0
    assert deps.score_stats.heuristic_kept == 1
    last = conn.execute("select reason from state_transitions where item_id=%s order by id desc limit 1", (iid,)).fetchone()
    assert "kept 30d" in last["reason"] and mode in last["reason"]


@pytest.mark.parametrize("mode", ["llm", "heuristic:prefilter", "heuristic:duplicate", "heuristic:no-llm", None])
def test_model_extracted_and_deterministic_junk_are_still_deleted(conn, fake_llm, fake_fx, mode):
    iid, deps = _scored_low(conn, fake_llm, fake_fx, mode)
    assert store.get_item(conn, iid) is None and deps.score_stats.heuristic_kept == 0


def test_kept_topic_cut_is_copied_to_the_shadow_set_at_100_percent(conn, fake_llm, fake_fx):
    iid, _ = _scored_low(conn, fake_llm, fake_fx, "heuristic:topic", shadow_rate_percent=10)   # rate 10 for deletions
    rows = conn.execute("select orig_item_id, kind, score from eval_shadow").fetchall()
    assert [(r["orig_item_id"], r["kind"], r["score"]) for r in rows] == [(iid, "topic", 10.0)]


def test_shadow_cap_is_per_kind(conn, fake_llm, fake_fx):
    for n in range(1, 4):          # fill the 'topic' kind (cap 2)
        _scored_low(conn, fake_llm, fake_fx, "heuristic:topic", mid=f"t{n}", shadow_rate_percent=100, shadow_max_rows=2)
    _scored_low(conn, fake_llm, fake_fx, "llm", mid="s1", shadow_rate_percent=100, shadow_max_rows=2)
    kinds = [r["kind"] for r in conn.execute("select kind from eval_shadow order by id").fetchall()]
    assert kinds.count("topic") == 2 and kinds.count("score") == 1      # topic rows did not starve the score kind


def test_kept_items_expire_after_30_days_and_only_those(conn, fake_llm, fake_fx):
    old, _ = _scored_low(conn, fake_llm, fake_fx, "heuristic:topic", mid="1")
    young, _ = _scored_low(conn, fake_llm, fake_fx, "heuristic:topic", mid="2")
    legacy, _ = _scored_low(conn, fake_llm, fake_fx, "heuristic:fallback", mid="3")
    ts_old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    for i in (old, legacy):
        conn.execute("UPDATE work_items SET updated_at=%s WHERE id=%s", (ts_old, i))
    surfaced = store.insert_item(conn, raw_text=POST + " s", source_channel="@c", source_message_id="4")
    conn.execute("UPDATE work_items SET state='surfaced', extract_mode='heuristic:topic', updated_at=%s WHERE id=%s", (ts_old, surfaced))
    conn.commit()
    assert store.purge_expired_heuristic(conn, 30) == 1
    assert store.get_item(conn, old) is None                            # expired
    assert store.get_item(conn, young) is not None                      # still within 30 days
    assert store.get_item(conn, legacy) is not None                     # legacy fallback awaits re-extract
    assert store.get_item(conn, surfaced) is not None                   # only 'rejected' topic rows expire


def test_ops_line_and_defaults():
    s = pipeline.ScoreRunStats(heuristic_kept=7, heuristic_purged=2)
    assert run.heuristic_line(s) == "heuristic extract: kept 7, expired 2"
    assert run.scoring_alert_text(s) is None and "heuristic extract: kept 7, expired 2" in run.parked_info_text(s)
    from job_hunter.config import Config
    assert Config().heuristic_keep_days == 30 and Deps().heuristic_keep_days == 30
