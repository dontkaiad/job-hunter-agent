"""Prompt-caching wiring tests (COST-ONLY change).

These verify that the CONSTANT system prefix is transmitted with
``cache_control`` where it qualifies (per-model minimum), that the per-item
VARIABLE vacancy content stays in the USER message (so the cached prefix is
byte-identical across items), and that caching never changes model output.

No real network: we either use the FakeLLM (which records the structured system
it would send) or mock ``AnthropicClient._client.messages.create`` directly.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from job_hunter import llm
from job_hunter.schema_extract import ExtractResult


# --- Pure policy: measured length vs per-model minimum -----------------------

def test_cache_min_tokens_by_model():
    assert llm.cache_min_tokens_for_model("claude-haiku-4-5") == 2048
    assert llm.cache_min_tokens_for_model("claude-sonnet-4-6") == 1024
    # Unknown / None -> conservative smaller floor.
    assert llm.cache_min_tokens_for_model(None) == 1024


def test_score_system_qualifies_for_cache_on_sonnet():
    """SCORE_SYSTEM exceeds Sonnet's ~1024-token minimum -> caches."""
    assert llm.estimate_tokens(llm.SCORE_SYSTEM) >= 1024
    assert llm.should_cache_system(llm.SCORE_SYSTEM, llm.JUDGE_MODEL) is True


def test_draft_and_extract_skip_cache_on_haiku_too_short():
    """DRAFT/EXTRACT are between 1024 and 2048 tokens, so they do NOT clear
    Haiku's ~2048 minimum and must be skipped (cache_control would not engage)."""
    draft_tokens = llm.estimate_tokens(llm.DRAFT_SYSTEM)
    extract_tokens = llm.estimate_tokens(llm.EXTRACT_SYSTEM)
    assert draft_tokens < 2048
    assert extract_tokens < 2048
    assert llm.should_cache_system(llm.DRAFT_SYSTEM, llm.CHEAP_MODEL) is False
    assert llm.should_cache_system(llm.EXTRACT_SYSTEM, llm.CHEAP_MODEL) is False
    assert llm.should_cache_system(llm.RESEARCH_SYSTEM, llm.CHEAP_MODEL) is False


# --- build_system_param: shape of the system field --------------------------

def test_build_system_param_plain_when_not_cached():
    out = llm.build_system_param("hello system", False)
    assert out == "hello system"  # back-compat: a plain string


def test_build_system_param_cached_is_block_list_with_cache_control():
    out = llm.build_system_param(llm.SCORE_SYSTEM, True)
    assert isinstance(out, list)
    assert len(out) == 1
    block = out[0]
    assert block["type"] == "text"
    assert block["cache_control"] == {"type": "ephemeral"}
    # The cached text is byte-identical to the constant system prompt.
    assert block["text"] == llm.SCORE_SYSTEM


# --- llm_score: cache_control present, variable content in USER msg ----------

def test_llm_score_sends_cached_system_block(fake_llm):
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 70, "Обоснование": "ok"}')
    ex = ExtractResult(title="t", source_channel="@c")
    raw = "UNIQUE_VACANCY_MARKER_SCORE_42 full posting body"
    llm.llm_score(fake_llm, ex, raw)

    call = fake_llm.calls[0]
    # cache_system flag flowed through.
    assert call["cache_system"] is True
    # The system field is a LIST containing a cached text block carrying the
    # constant SCORE_SYSTEM prompt.
    sp = call["system_param"]
    assert isinstance(sp, list)
    assert sp[0]["cache_control"] == {"type": "ephemeral"}
    assert sp[0]["text"] == llm.SCORE_SYSTEM


def test_llm_score_variable_text_in_user_not_in_cached_block(fake_llm):
    """The per-item vacancy text must live in the USER message, NOT in the
    cached system block, so the cached prefix is byte-identical across items."""
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 70, "Обоснование": "ok"}')
    ex = ExtractResult(title="t", source_channel="@c")
    raw = "UNIQUE_VACANCY_MARKER_SCORE_42 full posting body"
    llm.llm_score(fake_llm, ex, raw)

    call = fake_llm.calls[0]
    cached_text = call["system_param"][0]["text"]
    # Variable marker is in the user message...
    assert "UNIQUE_VACANCY_MARKER_SCORE_42" in call["user"]
    # ...and absent from the cached constant prefix.
    assert "UNIQUE_VACANCY_MARKER_SCORE_42" not in cached_text


def test_cached_prefix_byte_identical_across_items(fake_llm):
    """Two different vacancies in one run -> the cached system block text is
    identical (only the USER message varies)."""
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 50, "Обоснование": "x"}')
    ex = ExtractResult(title="t", source_channel="@c")
    llm.llm_score(fake_llm, ex, "vacancy ONE body")
    llm.llm_score(fake_llm, ex, "vacancy TWO body")

    block_a = fake_llm.calls[0]["system_param"][0]["text"]
    block_b = fake_llm.calls[1]["system_param"][0]["text"]
    assert block_a == block_b == llm.SCORE_SYSTEM
    assert fake_llm.calls[0]["user"] != fake_llm.calls[1]["user"]


# --- Skipped call sites: plain string system (no cache_control) --------------

def test_llm_extract_sends_plain_system_no_cache(fake_llm):
    fake_llm.set_for("strict JSON", '{"title":"X","stack":["python"]}')
    llm.llm_extract(fake_llm, "raw post", "@c", None)
    call = fake_llm.calls[0]
    assert call["cache_system"] is False
    assert call["system_param"] == llm.EXTRACT_SYSTEM  # plain string, not a list


def test_llm_draft_sends_plain_system_no_cache(fake_llm):
    fake_llm.set_for("application message", "Hi there.")
    ex = ExtractResult(title="t", source_channel="@c")
    llm.llm_draft(fake_llm, ex, "raw")
    call = fake_llm.calls[0]
    assert call["cache_system"] is False
    assert call["system_param"] == llm.DRAFT_SYSTEM  # plain string, not a list


# --- Output-identical guarantee ---------------------------------------------

def test_caching_does_not_change_score_output(fake_llm):
    """With the same mocked response, the parsed score + «Обоснование» are
    identical whether or not the system block is cached (caching is cost-only)."""
    resp = '{"relevance_score": 82, "Обоснование": "Сильный фит — 82/100"}'
    fake_llm.set_for("hiring-fit JUDGE", resp)
    ex = ExtractResult(title="t", source_channel="@c")
    out = llm.llm_score(fake_llm, ex, "raw")
    # Same result as parsing the response directly (no transport influence).
    assert out == llm.parse_score_response(resp)
    assert out["score"] == 82
    assert out["reasoning"] == "Сильный фит — 82/100"


# --- AnthropicClient.complete request construction (mocked transport) --------

def _make_client_with_mock():
    client = llm.AnthropicClient.__new__(llm.AnthropicClient)
    client.model = llm.JUDGE_MODEL
    client._client = MagicMock()
    # Anthropic-style response: .content is a list of blocks with .text
    client._client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(text="OK")]
    )
    return client


def test_anthropic_complete_builds_cached_system_kwarg():
    client = _make_client_with_mock()
    out = client.complete(
        llm.SCORE_SYSTEM,
        "VARIABLE_USER_BODY",
        max_tokens=400,
        model=llm.JUDGE_MODEL,
        cache_system=True,
    )
    assert out == "OK"
    kwargs = client._client.messages.create.call_args.kwargs
    # system kwarg is the cached-block LIST...
    assert isinstance(kwargs["system"], list)
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kwargs["system"][0]["text"] == llm.SCORE_SYSTEM
    # ...and the variable content is in messages[0]['content'], NOT the system.
    assert kwargs["messages"][0]["content"] == "VARIABLE_USER_BODY"
    assert "VARIABLE_USER_BODY" not in kwargs["system"][0]["text"]
    # model / max_tokens unchanged.
    assert kwargs["model"] == llm.JUDGE_MODEL
    assert kwargs["max_tokens"] == 400


def test_anthropic_complete_plain_system_when_not_cached():
    client = _make_client_with_mock()
    client.complete("SHORT SYS", "USER", cache_system=False)
    kwargs = client._client.messages.create.call_args.kwargs
    # Back-compat: a plain string system field when caching is off.
    assert kwargs["system"] == "SHORT SYS"
