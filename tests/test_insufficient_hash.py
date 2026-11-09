"""An `insufficient` vacancy is not sent to the model again until its INPUT changes.

Before this rule every harvest re-asked every parked vacancy (nothing recorded what the
model had read): ~105 calls a day for no new information.
"""
import importlib.util
import pathlib

import pytest

from job_hunter import pipeline, run, scoring, store, vacancy_fetch as vf
from job_hunter.pipeline import Deps

POST = ("Acme is hiring engineers across several teams, see the careers page for details. "
        "We are a friendly company based in Berlin building developer tools since 2015.")
INSUFFICIENT = {"relevance_score": None, "insufficient_content": True, "reasoning": "company blurb, no role"}
SCORE = lambda n: {"relevance_score": n, "insufficient_content": False, "reasoning": f"{n}/100"}


class ToolLLM:
    def __init__(self, *results):
        self.results, self.calls = list(results), 0

    def complete(self, *a, **k):  # pragma: no cover
        raise AssertionError("text path used")

    def complete_tool(self, *a, **k):
        self.calls += 1
        if not self.results:
            raise AssertionError("the model was called although the input did not change")
        return self.results.pop(0)


def _deps(client, fx, **kw):
    return Deps(llm_client=client, fx=fx, use_llm_extract=False, min_persist_score=25, **kw)


def _parked(conn, client, fx, **kw):
    deps = _deps(client, fx, **kw)
    iid = store.insert_item(conn, raw_text=POST, source_channel="@c", source_message_id="1")
    pipeline.advance_by_id(conn, iid, deps=deps)                  # extract
    pipeline.advance_by_id(conn, iid, deps=deps)                  # score -> insufficient
    return iid, deps


def test_hash_covers_both_texts_and_is_stable():
    h = scoring.input_hash(POST, None)
    assert h == scoring.input_hash(POST, "") == scoring.input_hash(POST)
    assert h != scoring.input_hash(POST + ".", None) and h != scoring.input_hash(POST, "page")
    assert scoring.input_hash("a", "bc") != scoring.input_hash("ab", "c")     # not a plain concatenation


def test_first_judgement_records_the_input_hash(conn, fake_fx):
    client = ToolLLM(INSUFFICIENT)
    iid, deps = _parked(conn, client, fake_fx)
    it = store.get_item(conn, iid)
    assert it.defer_reason == "insufficient" and it.defer_input_hash == scoring.input_hash(POST, None)
    assert client.calls == 1 and deps.score_stats.insufficient == 1 and it.score_attempts == 0


def test_unchanged_input_never_reaches_the_model_again(conn, fake_fx):
    client = ToolLLM(INSUFFICIENT)                                  # a 2nd call would raise
    iid, deps = _parked(conn, client, fake_fx)
    for _ in range(5):                                              # five more harvests
        res = pipeline.advance_by_id(conn, iid, deps=deps)
        assert res.status == "noop" and "input unchanged" in res.reason
    assert client.calls == 1
    assert deps.score_stats.insufficient_parked == 5
    it = store.get_item(conn, iid)
    assert it.state == "extracted" and it.score_attempts == 0 and it.relevance_score is None


def test_a_changed_fetched_text_is_scored_again_once(conn, fake_fx):
    client = ToolLLM(INSUFFICIENT, SCORE(80))
    iid, deps = _parked(conn, client, fake_fx)
    conn.execute("UPDATE work_items SET fetched_text=%s WHERE id=%s", ("Full role description. " * 40, iid))
    conn.commit()
    pipeline.run_to_gate(conn, iid, deps=deps)
    assert client.calls == 2                                         # exactly one more call
    it = store.get_item(conn, iid)
    assert it is not None and it.state != "extracted"               # scored, moved on
    assert it.defer_reason is None and it.defer_input_hash is None   # cleared with the move


def test_a_changed_raw_text_is_scored_again(conn, fake_fx):
    client = ToolLLM(INSUFFICIENT, INSUFFICIENT)
    iid, deps = _parked(conn, client, fake_fx)
    conn.execute("UPDATE work_items SET raw_text=%s WHERE id=%s", (POST + " Edited: now hiring in Munich too.", iid))
    conn.commit()
    pipeline.advance_by_id(conn, iid, deps=deps)
    assert client.calls == 2
    it = store.get_item(conn, iid)
    assert it.defer_reason == "insufficient"
    assert it.defer_input_hash == scoring.input_hash(POST + " Edited: now hiring in Munich too.", None)
    pipeline.advance_by_id(conn, iid, deps=deps)                     # and parked again on the new input
    assert client.calls == 2


def test_the_fetch_that_brings_the_page_triggers_exactly_one_rescore(conn, fake_fx):
    client = ToolLLM(INSUFFICIENT, SCORE(30))
    calls = []

    def fetch(raw, link, denylist=()):
        calls.append(1)
        return vf.FetchResult(vf.OK, url="https://acme.io/jobs/7", host="acme.io", text="Senior LLM engineer role. " * 30)

    iid, deps = _parked(conn, client, fake_fx, fetch_enabled=True, fetch_fn=fetch)
    # the first pass had no fetch (the post is long enough); on the next harvest the
    # parked item is fetched, the input changes, the model is asked once more
    pipeline.run_to_gate(conn, iid, deps=deps)
    assert len(calls) == 1 and client.calls == 2
    assert store.get_item(conn, iid) is None or store.get_item(conn, iid).state != "extracted"


def test_a_vacancy_parked_before_the_rule_is_asked_once_to_record_its_hash(conn, fake_fx):
    """No stored hash (parked before this change) -> one more call, which records it.
    The one-off script sets the hashes beforehand so this does not happen in production."""
    client = ToolLLM(INSUFFICIENT, INSUFFICIENT)
    iid, deps = _parked(conn, client, fake_fx)
    conn.execute("UPDATE work_items SET defer_input_hash = NULL WHERE id=%s", (iid,)); conn.commit()
    pipeline.advance_by_id(conn, iid, deps=deps)
    assert client.calls == 2 and store.get_item(conn, iid).defer_input_hash == scoring.input_hash(POST, None)
    pipeline.advance_by_id(conn, iid, deps=deps)
    assert client.calls == 2


def test_link_only_and_normal_vacancies_are_unaffected(conn, fake_fx):
    client = ToolLLM(SCORE(80))
    deps = _deps(client, fake_fx)
    iid = store.insert_item(conn, raw_text="Remote Python LLM engineer, RAG, Claude API, FastAPI. 4000 EUR. @hr_acme",
                            source_channel="@c", source_message_id="2")
    pipeline.run_to_gate(conn, iid, deps=deps)
    assert client.calls == 1


# --- the one-off script ------------------------------------------------------

def _script():
    spec = importlib.util.spec_from_file_location(
        "mih", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "oneoff" / "mark_insufficient_input_hash.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_oneoff_marks_existing_parked_items_without_touching_anything_else(conn, fake_fx):
    client = ToolLLM(INSUFFICIENT)
    iid, deps = _parked(conn, client, fake_fx)
    conn.execute("UPDATE work_items SET defer_input_hash = NULL WHERE id=%s", (iid,)); conn.commit()
    before = store.get_item(conn, iid)
    rows = conn.execute("SELECT id, raw_text, fetched_text, defer_input_hash FROM work_items WHERE defer_reason='insufficient'").fetchall()
    todo = _script().plan(rows)
    assert todo == [(iid, scoring.input_hash(POST, None))]
    store.set_input_hash(conn, iid, todo[0][1])
    after = store.get_item(conn, iid)
    assert after.defer_input_hash == todo[0][1]
    assert (after.state, after.updated_at, after.score_attempts, after.defer_reason) == \
           (before.state, before.updated_at, before.score_attempts, before.defer_reason)
    assert _script().plan(conn.execute("SELECT id, raw_text, fetched_text, defer_input_hash FROM work_items WHERE defer_reason='insufficient'").fetchall()) == []
    # then the next harvest does not call the model
    pipeline.advance_by_id(conn, iid, deps=deps)
    assert client.calls == 1


def test_the_oneoff_script_cannot_call_the_api():
    src = (pathlib.Path(__file__).resolve().parent.parent / "scripts" / "oneoff" / "mark_insufficient_input_hash.py").read_text()
    assert "anthropic" not in src.lower() and "AnthropicClient" not in src and "llm_score" not in src


# --- ops line ----------------------------------------------------------------

def test_ops_line_reports_parked_insufficient_separately():
    s = pipeline.ScoreRunStats(attempted=10, insufficient=2, insufficient_parked=105)
    assert run.scoring_alert_text(s) is None                       # never red
    info = run.parked_info_text(s)
    assert "insufficient parked 105 (no re-score)" in info and "deferred: link_only 0, insufficient 2" in info
    s.failed, s.kinds["parse"] = 5, 5
    assert "insufficient parked 105 (no re-score)" in run.scoring_alert_text(s)
