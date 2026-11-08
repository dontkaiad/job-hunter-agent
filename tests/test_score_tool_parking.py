"""Forced-tool scoring + parking of contentless posts (2026-11-01 incident).

Root cause: link-only stub posts made Haiku answer in PROSE ("I cannot access
external URLs"), which broke JSON parsing and burned score attempts (9 vacancies
at 2/3). Fixes under test: deterministic link_only gate (no LLM, no attempt),
forced-tool scoring with insufficient_content (parked, no attempt, never 0 /
deleted), and an alert that counts only real failures.
"""
import json
import os

import pytest

from job_hunter import llm, pipeline, run, scoring, store
from job_hunter.pipeline import Deps
from job_hunter.states import EXTRACTED

FIX = json.load(open(os.path.join(os.path.dirname(__file__), "..", "evals", "synthetic", "score_prose_refusals.json")))

GOOD_POST = ("Remote Senior Python engineer building LLM RAG agents with Claude API and "
             "FastAPI. Salary 250000-350000 RUB. Contact @hr_acme")


class ToolLLM:
    """Fake client exposing the tool path; scripted tool inputs / errors."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0
        self.tools = []
        self.temps = []

    def complete(self, *a, **k):  # pragma: no cover - must not be used on the tool path
        raise AssertionError("text path used")

    def complete_tool(self, system, user, tool, max_tokens=1024, model=None, cache_system=False,
                      temperature=None):
        self.calls += 1
        self.temps.append(temperature)
        self.tools.append((system, tool))
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _deps(client, fake_fx, **kw):
    return Deps(llm_client=client, fx=fake_fx, use_llm_extract=False,
                min_persist_score=25, **kw)


def _extracted_item(conn, deps, text=GOOD_POST, mid="1"):
    item_id = store.insert_item(conn, raw_text=text, source_channel="@c", source_message_id=mid)
    pipeline.advance_by_id(conn, item_id, deps=deps)  # T1 extract
    assert store.get_item(conn, item_id).state == EXTRACTED
    return item_id


# --- deterministic link_only gate -------------------------------------------

@pytest.mark.parametrize("stub", FIX["stubs"])
def test_stub_posts_are_link_only(stub):
    assert scoring.is_link_only(stub, 40)


def test_short_post_with_a_title_is_not_link_only():
    assert not scoring.is_link_only(FIX["real_short"], 40)


def test_url_less_one_liner_is_not_link_only():
    assert not scoring.is_link_only("Office Python Senior LLM role in Moscow. @hr", 40)


def test_link_only_never_reaches_llm_and_spends_no_attempt(conn, fake_fx):
    client = ToolLLM()
    deps = _deps(client, fake_fx, link_only_min_chars=40)
    item_id = _extracted_item(conn, deps, text=FIX["stubs"][1])

    res = pipeline.advance_by_id(conn, item_id, deps=deps)

    it = store.get_item(conn, item_id)
    assert res.status == "noop" and "link_only" in res.reason
    assert client.calls == 0
    assert it.state == EXTRACTED and it.defer_reason == "link_only"
    assert it.score_attempts == 0 and not it.score_failed and it.relevance_score is None
    assert deps.score_stats.link_only == 1 and deps.score_stats.failed == 0


# --- insufficient_content / null score --------------------------------------

@pytest.mark.parametrize("payload", [
    {"relevance_score": None, "insufficient_content": True, "reasoning": "only a link"},
    {"relevance_score": None, "insufficient_content": False, "reasoning": "?"},
    # model contradicts itself: a number AND insufficient -> still parked, never 10
    {"relevance_score": 10, "insufficient_content": True, "reasoning": "no content"},
])
def test_insufficient_content_parks_without_attempt_or_delete(conn, fake_fx, payload):
    client = ToolLLM(payload)
    deps = _deps(client, fake_fx)
    item_id = _extracted_item(conn, deps)

    res = pipeline.advance_by_id(conn, item_id, deps=deps)

    it = store.get_item(conn, item_id)
    assert it is not None, "must not be deleted"
    assert res.status == "noop"
    assert it.state == EXTRACTED and it.defer_reason == "insufficient"
    assert it.score_attempts == 0 and it.relevance_score is None  # no 0 written
    assert deps.score_stats.insufficient == 1
    assert deps.score_stats.failed == 0 and deps.score_stats.deferred == 0


def test_real_low_score_is_still_deleted(conn, fake_fx):
    client = ToolLLM({"relevance_score": 10, "insufficient_content": False, "reasoning": "wrong domain"})
    deps = _deps(client, fake_fx)
    item_id = _extracted_item(conn, deps)

    pipeline.run_to_gate(conn, item_id, deps=deps)

    assert store.get_item(conn, item_id) is None


def test_real_high_score_surfaces_and_uses_forced_tool(conn, fake_fx):
    client = ToolLLM({"relevance_score": 82, "insufficient_content": False, "reasoning": "fit 82/100"})
    deps = _deps(client, fake_fx)
    item_id = _extracted_item(conn, deps)

    results = pipeline.run_to_gate(conn, item_id, deps=deps)

    assert any(r.to_state == "surfaced" for r in results)
    system, tool = client.tools[0]
    assert tool["name"] == "submit_score" and "submit_score" in system
    assert store.get_item(conn, item_id).defer_reason is None


def test_parked_item_is_unparked_once_scored(conn, fake_fx):
    client = ToolLLM(
        {"relevance_score": None, "insufficient_content": True, "reasoning": "x"},
        {"relevance_score": 70, "insufficient_content": False, "reasoning": "ok 70/100"},
    )
    deps = _deps(client, fake_fx)
    item_id = _extracted_item(conn, deps)
    pipeline.advance_by_id(conn, item_id, deps=deps)
    assert store.get_item(conn, item_id).defer_reason == "insufficient"
    # Same input -> NOT retried (see tests/test_insufficient_hash.py); the page text
    # arriving changes the input, so the next harvest asks the model again.
    conn.execute("UPDATE work_items SET fetched_text=%s WHERE id=%s", ("The full role description. " * 30, item_id))
    conn.commit()
    pipeline.advance_by_id(conn, item_id, deps=deps)
    it = store.get_item(conn, item_id)
    assert it.state != EXTRACTED and it.defer_reason is None


# --- prose refusals / random refusal on a long text (id 11009) --------------

@pytest.mark.parametrize("case", FIX["refusals"], ids=lambda c: str(c["id"]))
def test_prose_refusal_is_still_a_parse_error_on_text_path(case):
    """Legacy text parser: prose can never become a score (and never a fake 0)."""
    with pytest.raises(ValueError):
        llm.parse_score_response(case["head"] + " ... " + case["tail"])


def test_tool_path_without_tool_block_is_a_counted_parse_failure(conn, fake_fx):
    """Long real text (like 11009), transport returns no tool_use -> real failure:
    attempt spent, kept, red-alert bucket 'parse'."""
    client = ToolLLM(ValueError("no tool_use block in response (stop_reason=end_turn)"))
    deps = _deps(client, fake_fx)
    long_post = GOOD_POST + " " + "Details about the role. " * 80
    item_id = _extracted_item(conn, deps, text=long_post)

    pipeline.advance_by_id(conn, item_id, deps=deps)

    it = store.get_item(conn, item_id)
    assert it.state == EXTRACTED and it.score_attempts == 1 and it.defer_reason is None
    assert deps.score_stats.kinds == {pipeline.FAIL_PARSE: 1}
    assert deps.score_stats.failed == 1


def test_parse_score_tool_input_rejects_garbage_score():
    with pytest.raises(ValueError):
        llm.parse_score_tool_input({"relevance_score": "high", "insufficient_content": False, "reasoning": ""})


def test_score_tool_schema_is_valid_for_anthropic():
    import re
    props = llm.SCORE_TOOL["input_schema"]["properties"]
    assert all(re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", k) for k in props)
    assert set(llm.SCORE_TOOL["input_schema"]["required"]) == set(props)


def test_tool_prompt_changes_only_the_answer_format():
    from job_hunter.profile import example_profile
    text = llm.build_score_system(example_profile())
    tool = llm.build_score_system(example_profile(), tool=True)
    assert tool.replace(llm._SCORE_TOOL_TAIL, llm._SCORE_JSON_TAIL) == text


# --- alert ------------------------------------------------------------------

def test_parked_items_never_make_the_alert_red():
    s = pipeline.ScoreRunStats(attempted=40, link_only=9, insufficient=3)
    assert run.scoring_alert_text(s) is None
    info = run.parked_info_text(s)
    assert "deferred: link_only 9, insufficient 3" in info and "\U0001f534" not in info


def test_red_alert_carries_the_parked_line_and_counts_only_real_failures():
    s = pipeline.ScoreRunStats(attempted=46, failed=12, deferred=12, link_only=9, insufficient=2)
    s.kinds["parse"] = 12
    text = run.scoring_alert_text(s)
    assert "12/46" in text and "deferred: link_only 9, insufficient 2" in text


def test_no_info_line_when_nothing_parked():
    assert run.parked_info_text(pipeline.ScoreRunStats(attempted=5)) is None


# --- AnthropicClient.complete_tool transport --------------------------------

class _Blk:
    def __init__(self, type_, **kw):
        self.type = type_
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content, stop_reason="tool_use"):
        self.content, self.stop_reason = content, stop_reason


def _client_with(resp):
    c = llm.AnthropicClient.__new__(llm.AnthropicClient)
    c.model = "m"
    seen = {}

    class _M:
        def create(self, **kw):
            seen.update(kw)
            return resp
    c._client = type("X", (), {"messages": _M()})()
    return c, seen


def test_complete_tool_forces_tool_choice_and_returns_input():
    blk = _Blk("tool_use", name="submit_score", input={"relevance_score": 55})
    c, seen = _client_with(_Resp([blk]))
    out = c.complete_tool("sys", "usr", llm.SCORE_TOOL, max_tokens=800)
    assert out == {"relevance_score": 55}
    assert seen["tool_choice"] == {"type": "tool", "name": "submit_score"}
    assert seen["tools"] == [llm.SCORE_TOOL]


def test_complete_tool_raises_on_prose_or_truncation():
    c, _ = _client_with(_Resp([_Blk("text", text="I cannot access external URLs")], "end_turn"))
    with pytest.raises(ValueError):
        c.complete_tool("s", "u", llm.SCORE_TOOL)
    blk = _Blk("tool_use", name="submit_score", input={"relevance_score": 5})
    c, _ = _client_with(_Resp([blk], "max_tokens"))
    with pytest.raises(ValueError):
        c.complete_tool("s", "u", llm.SCORE_TOOL)


# --- temperature -------------------------------------------------------------

def test_scoring_uses_temperature_zero_on_tool_path():
    from job_hunter.schema_extract import ExtractResult
    c = ToolLLM({"relevance_score": 50, "insufficient_content": False, "reasoning": "x"},
                {"relevance_score": 50, "insufficient_content": False, "reasoning": "x"})
    ex = ExtractResult(title="t", source_channel="c")
    llm.llm_score(c, ex, "text", model="claude-haiku-4-5")
    llm.llm_score(c, ex, "text", model="claude-sonnet-4-6")  # corridor judge
    assert c.temps == [0.0, 0.0]


def test_scoring_uses_temperature_zero_on_text_path(fake_llm):
    from job_hunter.schema_extract import ExtractResult
    seen = {}
    orig = fake_llm.complete
    fake_llm.complete = lambda *a, **k: seen.update(k) or '{"relevance_score": 40}'
    llm.llm_score(fake_llm, ExtractResult(title="t", source_channel="c"), "text")
    assert seen["temperature"] == 0.0


def test_non_scoring_steps_keep_default_temperature(fake_llm):
    # extract/research/draft must NOT be pinned to 0 (draft wants variety).
    import inspect
    assert inspect.signature(llm.AnthropicClient.complete).parameters["temperature"].default is None


def test_transport_sends_temperature_only_when_given():
    blk = _Blk("tool_use", name="submit_score", input={"relevance_score": 5})
    c, seen = _client_with(_Resp([blk]))
    c.complete_tool("s", "u", llm.SCORE_TOOL, temperature=0.0)
    assert seen["temperature"] == 0.0
    c, seen = _client_with(_Resp([blk]))
    c.complete_tool("s", "u", llm.SCORE_TOOL)
    assert "temperature" not in seen
