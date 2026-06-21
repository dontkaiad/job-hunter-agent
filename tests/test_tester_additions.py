"""Additional tests added by the Tester role to cover gaps identified in the
Tester validation pass against DESIGN.md and SCORING.md.

Coverage added:
  - T9 (backlog -> skipped): missing from test_pipeline.py
  - T3 via soft score (below threshold, not hard reject): missing
  - render_surfaced: must show BOTH original currency+amount AND rub-equivalent
  - render_surfaced: single-amount salary, salary_rub=None (no FX), RUB-native salary
  - salary_min-only (no salary_max) in pipeline._salary_max_rub
  - LLM-less T10/T11 (no LLM client -> needs_human)
  - Near-threshold without LLM client falls back to deterministic
  - advance() on an item with missing extracted_json returns noop, not crash
  - format_rub boundary at exactly 1000
  - FX: unknown currency returns None from FxRates.convert
  - Atomicity: both state_transitions and work_items updated in one commit
  - Dedup does not trigger for different channels
  - Bot gate fail-closed (empty allowed_user_ids): drops BOTH messages and callbacks
  - Bot gate fallback scenario: notify_chat_id acts as allowlist; other ids dropped
  - handle_callback fail-closed: in-handler guard also fires when allowed_user_ids=set()
  - render_surfaced: original currency code in salary line (not just rub-equivalent)
  [2nd pass additions]
  - Prefilter pipeline integration: "ищу работу" post reaches REJECTED via T3 with NO
    LLM judge call (prefilter-drop path in _do_score sets score=0 deterministically).
  - T12 bot wiring: handle_callback "send" on a DRAFTED item moves it to SENT (T12 HITL
    from the bot callback, complementing the pipeline-only test in test_pipeline.py).
  [3rd pass additions — validation-point gaps]
  - reconcile does not force remote=True when text has no remote signal (no-signal guard,
    prevents false over-correction when LLM said remote=False for an office-only job).
  - reconcile preserves a correct LLM-provided real body email (channel-leak guard does
    NOT strip a genuine recruiter email that was correctly extracted by the LLM).
  - ingest_web HTML parser captures the full message body including bottom-line contact
    (no length cap; long post bottom line reaches the IngestMessage.raw_text).
"""

from __future__ import annotations

import json

import pytest

from job_hunter import bot, pipeline, scoring, store
from job_hunter.fx import FxRates, format_rub
from job_hunter.pipeline import Deps
from job_hunter.schema_extract import ExtractResult
from job_hunter.states import (
    APPROVED,
    BACKLOG,
    CLOSED,
    DISCOVERED,
    DRAFTED,
    EXTRACTED,
    KIND_DETERMINISTIC,
    REJECTED,
    RESEARCHED,
    SCORED,
    SENT,
    SKIPPED,
    SURFACED,
    DECISION_APPROVE,
    DECISION_BACKLOG,
    DECISION_SEND,
    DECISION_SKIP,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk(**kw) -> ExtractResult:
    base = dict(title="t", source_channel="@c")
    base.update(kw)
    return ExtractResult(**base)


def _insert(conn, text, mid="1", channel="@c"):
    return store.insert_item(conn, raw_text=text, source_channel=channel,
                             source_message_id=mid)


GOOD_POST = (
    "Remote Senior Python engineer building LLM RAG agents with Claude API "
    "and FastAPI. Salary 250000-350000 RUB. Contact @hr_acme"
)

LOW_SCORE_OK_SALARY = (
    # Salary comfortably above the example profile floor (EUR 1000/mo = ~100k
    # RUB at the test FX rate of 100 RUB/EUR) so the low-score path is a SOFT
    # reject, not a salary hard reject.
    "Office-based Python developer, Moscow, PyTorch model training. "
    "Salary 300000 RUB. No remote. @hr"
)


def _surface(conn, deps, text=GOOD_POST, mid="1"):
    """Insert + run_to_gate; returns item_id in SURFACED state."""
    item_id = _insert(conn, text, mid=mid)
    pipeline.run_to_gate(conn, item_id, deps=deps)
    assert store.get_item(conn, item_id).state == SURFACED
    return item_id


# ---------------------------------------------------------------------------
# T9: backlog -> skipped (missing transition)
# ---------------------------------------------------------------------------

def test_t9_backlog_to_skipped(conn, deps):
    """T9 must move an item from BACKLOG to SKIPPED when human decides skip."""
    item_id = _surface(conn, deps, mid="t9")
    # T6: surfaced -> backlog
    r = pipeline.advance_by_id(conn, item_id, decision=DECISION_BACKLOG, deps=deps)
    assert r.to_state == BACKLOG

    # T9: backlog -> skipped
    r2 = pipeline.advance_by_id(conn, item_id, decision=DECISION_SKIP, deps=deps)
    assert r2.to_state == SKIPPED
    assert r2.status == "moved"

    # Verify it is now terminal
    r3 = pipeline.advance_by_id(conn, item_id, deps=deps)
    assert r3.status == "terminal"

    # Audit log must contain the skipped transition
    trans = store.list_transitions(conn, item_id)
    to_states = [t["to_state"] for t in trans]
    assert BACKLOG in to_states
    assert SKIPPED in to_states


# ---------------------------------------------------------------------------
# T3 via soft score (below threshold, NOT hard reject)
# ---------------------------------------------------------------------------

def test_t3_score_reject_not_hard_reject(conn, fake_llm, fake_fx):
    """Items below SURFACE_THRESHOLD that are NOT a salary hard-reject must
    still reach REJECTED via T3 (soft score path).

    Here the Sonnet judge returns a LOW relevance score (anti-target role)
    while the salary is comfortably above the floor -> soft reject, not a
    salary hard reject.
    """
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.set_for(
        "hiring-fit JUDGE",
        '{"relevance_score": 25, "Обоснование": "классическое обучение моделей, офис"}',
    )
    item_id = _insert(conn, LOW_SCORE_OK_SALARY, mid="soft_reject")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == REJECTED
    assert item.relevance_score is not None
    assert item.relevance_score < scoring.SURFACE_THRESHOLD

    trans = store.list_transitions(conn, item_id)
    reasons = [t["reason"] or "" for t in trans]
    # Should NOT be a hard reject
    assert not any("hard reject" in r.lower() for r in reasons)
    # The transition reason should mention score
    assert any("score" in r.lower() for r in reasons)


# ---------------------------------------------------------------------------
# render_surfaced: BOTH original currency+amount AND rub-equivalent (high priority)
# ---------------------------------------------------------------------------

def test_render_surfaced_shows_original_currency_and_rub(conn):
    """SCORING.md: display must contain BOTH original currency amount AND
    rub-equivalent side by side, e.g. '€3000 (~290k ₽)'.
    The original must never be replaced by the converted value alone."""
    item_id = _insert(conn, "x", mid="rs1")
    ex = {
        "title": "AI Eng",
        "salary_min": 2000,
        "salary_max": 3000,
        "currency": "EUR",
        "remote": True,
        "reasons": [],
    }
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex), relevance_score=72.0)
    item = store.get_item(conn, item_id)
    text = bot.render_surfaced(item, salary_rub=290000)

    # The original amount must appear.
    assert "2000" in text, "original salary_min missing from rendered card"
    assert "3000" in text, "original salary_max missing from rendered card"
    # The original currency code must appear.
    assert "EUR" in text, "original currency 'EUR' missing from rendered card"
    # The RUB-equivalent must also appear.
    assert "₽" in text, "RUB-equivalent missing from rendered card"
    # The salary line must have the canonical format.
    salary_line = next((l for l in text.splitlines() if "Salary" in l), "")
    assert "EUR" in salary_line, f"original currency not in salary line: {salary_line!r}"
    assert "₽" in salary_line, f"rub-equivalent not in salary line: {salary_line!r}"


def test_render_surfaced_single_amount_shows_original_and_rub(conn):
    """Single-value salary (min==max) must also show both original and rub."""
    item_id = _insert(conn, "x", mid="rs2")
    ex = {"title": "Eng", "salary_min": 3000, "salary_max": 3000, "currency": "USD"}
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex), relevance_score=50.0)
    item = store.get_item(conn, item_id)
    text = bot.render_surfaced(item, salary_rub=270000)

    assert "3000" in text
    assert "USD" in text
    assert "₽" in text


def test_render_surfaced_no_rub_shows_original_without_rub(conn):
    """When salary_rub is None (FX unavailable), show the original salary
    without a rub-equivalent — must NOT show '₽' in salary line."""
    item_id = _insert(conn, "x", mid="rs3")
    ex = {"title": "Eng", "salary_min": 5000, "salary_max": 8000, "currency": "EUR"}
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex), relevance_score=60.0)
    item = store.get_item(conn, item_id)
    text = bot.render_surfaced(item, salary_rub=None)

    salary_line = next((l for l in text.splitlines() if "Salary" in l), "")
    assert "EUR" in salary_line, "original currency must appear even without FX"
    # No rub-equivalent expected
    assert "₽" not in salary_line


def test_render_surfaced_rub_native_salary(conn):
    """When salary is already in RUB, show only the RUB amount (no conversion)."""
    item_id = _insert(conn, "x", mid="rs4")
    ex = {"title": "Eng", "salary_min": 200000, "salary_max": 300000, "currency": "RUB"}
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex), relevance_score=70.0)
    item = store.get_item(conn, item_id)
    # When salary is RUB, salary_rub can be passed to show as rub-equivalent too
    text = bot.render_surfaced(item, salary_rub=300000)
    salary_line = next((l for l in text.splitlines() if "Salary" in l), "")
    assert "RUB" in salary_line or "300000" in salary_line


def test_render_surfaced_unknown_salary(conn):
    """When salary is entirely absent, show 'Salary: unknown'."""
    item_id = _insert(conn, "x", mid="rs5")
    ex = {"title": "Eng", "salary_min": None, "salary_max": None, "currency": None}
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex), relevance_score=40.0)
    item = store.get_item(conn, item_id)
    text = bot.render_surfaced(item, salary_rub=None)
    assert "unknown" in text.lower()


# ---------------------------------------------------------------------------
# salary_min only (no salary_max) in _salary_max_rub
# ---------------------------------------------------------------------------

def test_pipeline_salary_min_only_used_for_prefilter(conn, deps):
    """When only salary_min is present (no salary_max), the min acts as top
    for the hard-reject check (SCORING.md: 'верх вилки').

    We inject the extracted_json directly (bypassing heuristic extract) so
    the test is not sensitive to regex parse rules.
    """
    item_id = _insert(conn, "some internship text", mid="so1")
    # Manually move to EXTRACTED with salary_min=130000, no salary_max
    ex_json = json.dumps({
        "title": "Python Intern",
        "source_channel": "@c",
        "stack": ["python"],
        "salary_min": 50000,
        "salary_max": None,
        "currency": "RUB",
        "remote": None,
        "reasons": [],
    })
    store.update_state(conn, item_id, EXTRACTED, from_state=DISCOVERED,
                       kind=KIND_DETERMINISTIC, actor="system",
                       reason="injected", extracted_json=ex_json)
    # T2 and T3/T4 via advance
    pipeline.advance_by_id(conn, item_id, deps=deps)  # T2: score
    pipeline.advance_by_id(conn, item_id, deps=deps)  # T3: reject/surface
    item = store.get_item(conn, item_id)
    # 50k RUB as top (salary_min used as fallback) -> below 100k floor
    # (example EUR 1000 * 100 RUB/EUR) -> rejected
    assert item.state == REJECTED
    trans = store.list_transitions(conn, item_id)
    assert any("hard reject" in (t["reason"] or "").lower() for t in trans)


def test_salary_max_rub_uses_salary_min_when_max_absent():
    """pipeline._salary_max_rub uses salary_min as fallback when salary_max is None."""
    from job_hunter.pipeline import _salary_max_rub, Deps
    from job_hunter.schema_extract import ExtractResult

    class StaticFx:
        def convert(self, amount, currency):
            return float(amount) * 90.0 if currency == "USD" else None

    deps = Deps(fx=StaticFx())
    ex = ExtractResult(title="t", source_channel="@c",
                       salary_min=1000.0, salary_max=None, currency="USD")
    result = _salary_max_rub(ex, deps)
    # salary_min 1000 USD * 90 = 90000 RUB
    assert result == pytest.approx(90000.0)


# ---------------------------------------------------------------------------
# T10/T11 without LLM client -> needs_human
# ---------------------------------------------------------------------------

def test_t10_research_without_llm_returns_needs_human(conn):
    """When no LLM client is in deps, T10 must return needs_human, not crash."""
    deps_no_llm = Deps(llm_client=None, fx=None, use_llm_extract=False)
    item_id = _insert(conn, GOOD_POST, mid="t10_nollm")
    pipeline.run_to_gate(conn, item_id, deps=Deps(use_llm_extract=False))
    pipeline.advance_by_id(conn, item_id, decision=DECISION_APPROVE,
                           deps=deps_no_llm)
    # Now in APPROVED state; advance without LLM
    res = pipeline.advance_by_id(conn, item_id, deps=deps_no_llm)
    assert res.status == "needs_human"
    assert store.get_item(conn, item_id).state == APPROVED


def test_t11_draft_without_llm_returns_needs_human(conn, deps, fake_llm):
    """When no LLM client is in deps at T11, must return needs_human."""
    fake_llm.set_for("research", '{"summary":"s","talking_points":[],"questions":[]}')
    item_id = _insert(conn, GOOD_POST, mid="t11_nollm")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    pipeline.advance_by_id(conn, item_id, decision=DECISION_APPROVE, deps=deps)
    pipeline.advance_by_id(conn, item_id, deps=deps)  # T10 research with LLM
    assert store.get_item(conn, item_id).state == RESEARCHED

    # Now advance T11 without LLM client
    deps_no_llm = Deps(llm_client=None, fx=None, use_llm_extract=False)
    res = pipeline.advance_by_id(conn, item_id, deps=deps_no_llm)
    assert res.status == "needs_human"
    assert store.get_item(conn, item_id).state == RESEARCHED


# ---------------------------------------------------------------------------
# Near-threshold without LLM -> falls back to deterministic score
# ---------------------------------------------------------------------------

def test_no_llm_judge_surfaces_for_manual_review(conn):
    """When no LLM judge is configured, T2 must NOT silently reject; it scores
    at the surface threshold so a human still sees the item."""
    deps_no_llm = Deps(llm_client=None, fx=None, use_llm_extract=False)

    item_id = _insert(conn, "Python FastAPI remote LLM role. Salary 200000 RUB. @hr",
                      mid="nt_nollm")
    pipeline.advance_by_id(conn, item_id, deps=deps_no_llm)  # T1 extract
    res2 = pipeline.advance_by_id(conn, item_id, deps=deps_no_llm)  # T2 score
    assert res2.to_state == SCORED
    item = store.get_item(conn, item_id)
    assert item.relevance_score == float(scoring.SURFACE_THRESHOLD)

    res3 = pipeline.advance_by_id(conn, item_id, deps=deps_no_llm)  # T3/T4
    assert res3.to_state == SURFACED
    assert "tiebreak" not in (res3.reason or "").lower()


# ---------------------------------------------------------------------------
# advance() on item with missing extracted_json returns noop gracefully
# ---------------------------------------------------------------------------

def test_advance_score_with_missing_extracted_json_returns_noop(conn):
    """If extracted_json is missing when T2 runs, advance must return noop,
    not raise an exception."""
    item_id = _insert(conn, "some text", mid="missing_ej")
    # Manually move to EXTRACTED state without setting extracted_json
    store.update_state(conn, item_id, EXTRACTED,
                       from_state=DISCOVERED, kind=KIND_DETERMINISTIC,
                       actor="system", reason="test", extracted_json=None)
    # T2 attempt: item is EXTRACTED but extracted_json is NULL
    res = pipeline.advance_by_id(conn, item_id, deps=Deps())
    assert res.status == "noop"
    assert store.get_item(conn, item_id).state == EXTRACTED  # state unchanged


# ---------------------------------------------------------------------------
# format_rub boundary
# ---------------------------------------------------------------------------

def test_format_rub_exactly_1000():
    """1000 RUB is on the boundary; must render as ~1k ₽."""
    assert format_rub(1000) == "~1k ₽"


def test_format_rub_999():
    """999 RUB is below 1000; must render without k suffix."""
    assert format_rub(999) == "~999 ₽"


# ---------------------------------------------------------------------------
# FX: unknown currency returns None
# ---------------------------------------------------------------------------

def test_fxrates_unknown_currency_returns_none():
    """FxRates.convert must return None for currencies not in the rate table."""
    def fake_get(url):
        class R:
            def raise_for_status(self): pass
            def json(self): return {"rates": {"USD": 0.011}}
        return R()

    fx = FxRates(provider="frankfurter", cache_ttl=86400, http_get=fake_get)
    result = fx.convert(1000, "JPY")
    assert result is None


def test_convert_rub_identity():
    """FxRates.convert(amount, 'RUB') must be identity (1:1)."""
    def fake_get(url):
        class R:
            def raise_for_status(self): pass
            def json(self): return {"rates": {"USD": 0.011}}
        return R()

    fx = FxRates(provider="frankfurter", cache_ttl=86400, http_get=fake_get)
    assert fx.convert(100000, "RUB") == pytest.approx(100000.0)


# ---------------------------------------------------------------------------
# Dedup does not trigger for different channels
# ---------------------------------------------------------------------------

def test_dedup_same_message_id_different_channel_not_deduped(conn):
    """Same source_message_id in different channels must NOT be deduplicated."""
    a = store.insert_item(conn, raw_text="post", source_channel="@chan_a",
                          source_message_id="42")
    b = store.insert_item(conn, raw_text="post", source_channel="@chan_b",
                          source_message_id="42")
    assert a is not None
    assert b is not None
    assert a != b


# ---------------------------------------------------------------------------
# Scoring: hard salary guard overrides the (LLM) relevance score
# ---------------------------------------------------------------------------

def test_salary_guard_unknown_salary_not_rejected():
    """Unknown salary (None RUB-equiv) must NOT be a hard reject."""
    assert scoring.salary_guard_reject(None, 250_000.0) is False


def test_salary_guard_below_floor_rejected():
    """A known RUB-equiv top below the floor is a deterministic reject."""
    assert scoring.salary_guard_reject(140_000.0, 250_000.0) is True


# ---------------------------------------------------------------------------
# Atomicity: update_state writes both work_items and state_transitions
# ---------------------------------------------------------------------------

def test_update_state_writes_both_tables_in_one_commit(conn):
    """update_state must update work_items.state AND insert a state_transitions
    row. If either is missing, the pipeline audit log is broken."""
    item_id = store.insert_item(conn, raw_text="x", source_channel="@c",
                                source_message_id="atomic1")
    store.update_state(conn, item_id, EXTRACTED,
                       from_state=DISCOVERED, kind=KIND_DETERMINISTIC,
                       actor="system", reason="test atomic")

    item = store.get_item(conn, item_id)
    assert item.state == EXTRACTED

    trans = store.list_transitions(conn, item_id)
    to_states = [t["to_state"] for t in trans]
    assert EXTRACTED in to_states
    matching = [t for t in trans if t["to_state"] == EXTRACTED]
    assert matching[0]["reason"] == "test atomic"
    assert matching[0]["from_state"] == DISCOVERED
    assert matching[0]["kind"] == KIND_DETERMINISTIC


# ---------------------------------------------------------------------------
# Bot gate: fail-closed with empty allowed_user_ids (CHANGE 2)
# ---------------------------------------------------------------------------

import asyncio as _asyncio

from job_hunter.bot import JobHunterBot, is_allowed
from job_hunter.config import Config


def _run_gate_pure(b, event):
    """Invoke the bot's access gate directly; returns (handler_ran, result)."""
    gate = b._make_access_gate()
    ran = {"hit": False}

    async def handler(event, data):
        ran["hit"] = True
        return "ran"

    result = _asyncio.run(gate(handler, event, {}))
    return ran["hit"], result


class _FakeUser2:
    def __init__(self, uid):
        self.id = uid


class _FakeMsg2:
    """Bare message stub (no .data attr)."""
    def __init__(self, uid):
        self.from_user = _FakeUser2(uid)


class _FakeCb2:
    """Callback stub."""
    def __init__(self, uid):
        self.from_user = _FakeUser2(uid)
        self.data = "jh:skip:1"
        self.answer_calls = 0

    async def answer(self, text=None):
        self.answer_calls += 1


def _bot_with(allowed_user_ids, conn, deps):
    cfg = Config(bot_token="x", notify_chat_id=555, allowed_user_ids=allowed_user_ids)
    return JobHunterBot(cfg, conn, deps)


def test_gate_fail_closed_drops_message_when_empty_set(conn, deps):
    """When allowed_user_ids is the empty set, the gate must drop all messages
    (fail-closed: no user is allowed, handler must NOT run)."""
    b = _bot_with(set(), conn, deps)
    msg = _FakeMsg2(uid=555)
    ran, result = _run_gate_pure(b, msg)
    assert ran is False, "handler must NOT run when allowed_user_ids is empty"
    assert result is None


def test_gate_fail_closed_drops_callback_when_empty_set(conn, deps):
    """When allowed_user_ids is the empty set, the gate must drop all callbacks."""
    b = _bot_with(set(), conn, deps)
    cb = _FakeCb2(uid=555)
    ran, result = _run_gate_pure(b, cb)
    assert ran is False
    assert result is None
    # Spinner silenced with empty answer.
    assert cb.answer_calls == 1


def test_gate_fallback_allows_notify_chat_id_only(conn, deps):
    """Fallback scenario: ALLOWED_USER_IDS unset -> allowed_user_ids = {notify_chat_id}.
    The notify_chat_id user must be allowed; any other user must be dropped.
    (Config-level already tested in test_config_allowlist; this tests the gate wiring.)"""
    # Simulate the fallback: allowed_user_ids contains exactly notify_chat_id.
    notify_id = 555
    b = _bot_with({notify_id}, conn, deps)

    # notify_chat_id user: allowed
    msg_ok = _FakeMsg2(uid=notify_id)
    ran, result = _run_gate_pure(b, msg_ok)
    assert ran is True, f"notify_chat_id {notify_id} must be allowed through gate"
    assert result == "ran"

    # Different user: dropped
    msg_other = _FakeMsg2(uid=999)
    ran2, result2 = _run_gate_pure(b, msg_other)
    assert ran2 is False
    assert result2 is None


def test_handle_callback_fail_closed_in_handler_guard(conn, deps):
    """The in-handler is_allowed guard in handle_callback (line 285 of bot.py)
    must also fire when called directly with a non-allowed user, even if
    middleware ran first. No business logic must execute."""
    b = _bot_with({777}, conn, deps)

    class FakeInHandlerCb:
        data = "jh:skip:999"
        from_user = _FakeUser2(uid=1)  # uid not in {777}
        answer_calls = 0
        message = type("M", (), {"edit_reply_markup": lambda self, **k: None})()

        async def answer(self, text=None):
            self.answer_calls += 1

    cb = FakeInHandlerCb()
    status = _asyncio.run(b.handle_callback(cb))
    assert status == "forbidden"
    assert cb.answer_calls == 1  # spinner silenced, no business ack text


# ---------------------------------------------------------------------------
# render_surfaced: original currency code present in salary line (SCORING.md)
# ---------------------------------------------------------------------------

def test_render_surfaced_original_currency_in_salary_line(conn):
    """SCORING.md: salary line must contain the original currency code, not just
    the rub-equivalent. This complements test_bot.py::test_render_surfaced_includes_score_and_salary
    which only checks the rub side."""
    item_id = _insert(conn, "x", mid="orig_ccy")
    ex = {
        "title": "Eng",
        "salary_min": 2000,
        "salary_max": 3000,
        "currency": "EUR",
        "reasons": [],
    }
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex), relevance_score=70.0)
    item = store.get_item(conn, item_id)
    text = bot.render_surfaced(item, salary_rub=290000)
    salary_line = next((l for l in text.splitlines() if "Salary" in l), "")

    # Must contain original amount and currency.
    assert "EUR" in salary_line, f"Original currency 'EUR' missing from: {salary_line!r}"
    assert "2000" in salary_line or "3000" in salary_line, (
        f"Original amount missing from: {salary_line!r}"
    )
    # Must also contain the rub-equivalent.
    assert "₽" in salary_line, f"RUB-equivalent missing from: {salary_line!r}"


# ---------------------------------------------------------------------------
# Prefilter pipeline integration (2nd pass: T2 -> T3 without any LLM call)
# ---------------------------------------------------------------------------

def test_prefilter_drop_reaches_rejected_without_llm_call(conn):
    """A 'ищу работу' (looking-for-work) post must reach REJECTED deterministically
    via the prefilter path in T2, with ZERO Sonnet judge calls.

    SCORING.md: the prefilter drops only obvious non-jobs; the pipeline's _do_score
    sets score=0 and reasoning='prefilter: ...' when prefilter rejects, bypassing the
    LLM judge entirely. T3 then rejects based on score < SURFACE_THRESHOLD.
    This confirms:
      - No LLM call is made for junk posts (cost + latency saving).
      - The deterministic path from T2 to T3-rejected is exercised end-to-end.
    """
    call_count = {"n": 0}

    class SpyLLM:
        def complete(self, *a, **kw):
            call_count["n"] += 1
            return '{"relevance_score": 80, "Обоснование": "should not be called"}'

    deps_spy = Deps(llm_client=SpyLLM(), fx=None, use_llm_extract=False)

    # A clearly non-job "looking for work" post (long enough to pass length check).
    resume_post = (
        "Ищу работу Python разработчиком, удалённо. "
        "5 лет опыта, FastAPI, Docker. #резюме @me_cv"
    )
    item_id = _insert(conn, resume_post, mid="pf_drop")
    pipeline.advance_by_id(conn, item_id, deps=deps_spy)  # T1 heuristic extract
    pipeline.advance_by_id(conn, item_id, deps=deps_spy)  # T2 score (prefilter drop)
    pipeline.advance_by_id(conn, item_id, deps=deps_spy)  # T3/T4

    item = store.get_item(conn, item_id)
    assert item.state == REJECTED, (
        f"Expected REJECTED for 'ищу работу' post; got {item.state}"
    )
    # The LLM judge must NOT have been called (prefilter short-circuits before Sonnet).
    assert call_count["n"] == 0, (
        f"Expected 0 LLM calls for prefilter-dropped post; got {call_count['n']}"
    )
    # Transition log must record the prefilter reason.
    trans = store.list_transitions(conn, item_id)
    all_reasons = " ".join(t["reason"] or "" for t in trans)
    assert "prefilter" in all_reasons.lower(), (
        f"Expected 'prefilter' in transition reasons; got: {all_reasons!r}"
    )


# ---------------------------------------------------------------------------
# T12 bot wiring: handle_callback "send" on DRAFTED -> SENT
# ---------------------------------------------------------------------------

def test_handle_callback_send_moves_drafted_to_sent(conn, fake_llm, fake_fx):
    """T12 bot wiring: the 'send' callback on a DRAFTED item must call
    pipeline.advance_by_id with DECISION_SEND and move the item to SENT.

    This exercises the bot's handle_callback wiring for T12 (HITL send), which
    is the only transition not covered by an end-to-end bot callback test. The
    pipeline-level T12 is covered by test_full_path_approve_research_draft_send_close
    in test_pipeline.py; this covers the bot-callback routing path.
    """
    import asyncio as _a

    from job_hunter.bot import JobHunterBot
    from job_hunter.config import Config
    from job_hunter.states import DRAFTED, SENT

    # Prime the LLM to produce a high score so the post surfaces.
    fake_llm.set_for(
        "hiring-fit JUDGE",
        '{"relevance_score": 85, "Обоснование": "applied-LLM, remote"}',
    )
    fake_llm.set_for("research", '{"summary":"s","talking_points":[],"questions":[]}')
    fake_llm.set_for("application message", "Dear recruiter, I am interested.")

    deps_full = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)

    # Build item and drive it all the way to DRAFTED.
    item_id = _insert(conn, GOOD_POST, mid="t12_bot")
    pipeline.run_to_gate(conn, item_id, deps=deps_full)   # -> SURFACED
    pipeline.advance_by_id(conn, item_id, decision=DECISION_APPROVE, deps=deps_full)  # -> APPROVED
    pipeline.advance_by_id(conn, item_id, deps=deps_full)  # T10 research -> RESEARCHED
    pipeline.advance_by_id(conn, item_id, deps=deps_full)  # T11 draft -> DRAFTED

    assert store.get_item(conn, item_id).state == DRAFTED

    # Simulate the "send" inline-button callback.
    ALLOWED_UID = 777
    cfg = Config(bot_token="x", notify_chat_id=123, allowed_user_ids={ALLOWED_UID})
    b = JobHunterBot(cfg, conn, deps_full)

    class _FakeUser:
        id = ALLOWED_UID

    class _FakeMsg:
        html_text = "#1  DRAFT for: AI Eng\n\nDear recruiter, I am interested."
        text = html_text

        def __init__(self):
            self.edit_text_calls = 0
            self.last_text = None
            self.last_markup = "UNSET"
            self.markup_cleared = False

        async def edit_text(self, text, reply_markup="UNSET", parse_mode=None):
            self.edit_text_calls += 1
            self.last_text = text
            self.last_markup = reply_markup
            self.markup_cleared = reply_markup is None

        async def edit_reply_markup(self, reply_markup=None):
            self.markup_cleared = reply_markup is None

    class _FakeSendCb:
        data = bot.encode_callback("send", item_id)
        from_user = _FakeUser()
        message = _FakeMsg()
        answered = None
        answer_calls = 0

        async def answer(self, text=None):
            self.answer_calls += 1
            self.answered = text

    cb = _FakeSendCb()
    status = _a.run(b.handle_callback(cb))

    assert status == "moved", f"Expected 'moved'; got {status!r}"
    assert store.get_item(conn, item_id).state == SENT, (
        f"Expected SENT after 'send' callback; got {store.get_item(conn, item_id).state}"
    )
    assert cb.answered == "Sent", f"Expected ack 'Sent'; got {cb.answered!r}"
    # «✅ Отправила» is a MANUAL-CONFIRM: the card is finalised to the
    # «✅ Отправлено» line and the keyboard is stripped (no longer actionable).
    assert cb.message.edit_text_calls == 1
    assert "✅ Отправлено" in cb.message.last_text
    assert cb.message.markup_cleared is True


# ---------------------------------------------------------------------------
# 3rd pass: gap tests added by Tester
# ---------------------------------------------------------------------------

# --- Gap 1: no-op path strips keyboard without editing text ----------------
#
# When handle_callback is called on an item already in a terminal state (e.g.
# operator double-taps a button), advance returns "terminal" (or "noop") and
# the code takes the else-branch:
#     else:
#         await self._strip_keyboard(cb)
# No test previously covered this path: prior tests only tested the "moved"
# branch where _finalize_card (edit_text + keyboard removal) is called.

def test_handle_callback_terminal_state_strips_keyboard_not_edits_text(conn, deps):
    """When an item is already in a terminal state, handle_callback must strip
    the keyboard (stop the spinner, remove buttons) but must NOT edit the card
    text (no edit_text call). The status returned is 'terminal'."""
    from tests.test_serve_and_buttons import (
        FakeCallback, FakeFinalizeMessage, _cfg, ALLOWED_UID,
    )

    # Drive item to SKIPPED (terminal) first.
    item_id = _surface(conn, deps, mid="term_strip")
    pipeline.advance_by_id(conn, item_id, decision=DECISION_SKIP, deps=deps)
    assert store.get_item(conn, item_id).state == SKIPPED

    b = bot.JobHunterBot(_cfg(), conn, deps)
    msg = FakeFinalizeMessage()
    # Try to approve an already-skipped item (illegal -> terminal).
    cb = FakeCallback(bot.encode_callback("approve", item_id), message=msg)

    status = _asyncio.run(b.handle_callback(cb))

    # advance returned "terminal"; the else-branch ran _strip_keyboard.
    assert status == "terminal"
    # _strip_keyboard was called -> edit_reply_markup with None.
    assert msg.markup_cleared is True
    # _finalize_card was NOT called -> edit_text must NOT have been called.
    assert msg.edit_text_calls == 0
    # State must remain SKIPPED (no regression).
    assert store.get_item(conn, item_id).state == SKIPPED


# --- Gap 2: card edit ordering - advance happens before edit_text ----------
#
# handle_callback sequence is: advance_by_id -> (run_to_gate on approve) ->
# cb.answer -> _finalize_card (edit_text) -> notify_draft.
# We verify that by the time edit_text is called the state has ALREADY been
# advanced (the store reflects the new state before the card is edited).

def test_handle_callback_advance_happens_before_card_edit(conn, deps):
    """When skip is pressed, the DB state must already be SKIPPED by the time
    edit_text is called on the message. This guards against a hypothetical
    regression where editing happened before the state write."""
    from job_hunter.states import SKIPPED as _SKIPPED

    ALLOWED_UID_LOCAL = 777

    item_id = _surface(conn, deps, mid="order_check")
    b = _bot_with({ALLOWED_UID_LOCAL}, conn, deps)

    state_at_edit_time = {}

    class OrderTrackingMessage:
        html_text = "🟢 85/100 — AI Eng"
        text = html_text
        markup_cleared = False
        edit_text_calls = 0
        last_edit_text = None
        last_edit_markup = "UNSET"

        async def edit_text(self, text, reply_markup="UNSET", parse_mode=None):
            self.edit_text_calls += 1
            # Capture the DB state AT THE MOMENT edit_text is called.
            state_at_edit_time["state"] = store.get_item(conn, item_id).state
            self.last_edit_text = text
            self.last_edit_markup = reply_markup

        async def edit_reply_markup(self, reply_markup=None):
            self.markup_cleared = reply_markup is None

    from tests.test_serve_and_buttons import FakeCallback

    msg = OrderTrackingMessage()
    cb = FakeCallback(bot.encode_callback("skip", item_id),
                      user_id=ALLOWED_UID_LOCAL, message=msg)

    _asyncio.run(b.handle_callback(cb))

    # edit_text was called.
    assert msg.edit_text_calls == 1
    # The DB state was already SKIPPED when edit_text executed.
    assert state_at_edit_time.get("state") == _SKIPPED, (
        f"Expected state=SKIPPED at edit_text call time; "
        f"got {state_at_edit_time.get('state')!r}"
    )


# ---------------------------------------------------------------------------
# 4th pass: reconcile precedence edge cases (validation-point gaps)
# ---------------------------------------------------------------------------

from job_hunter import extract as _extract_mod
from job_hunter.schema_extract import from_dict as _from_dict


def test_reconcile_no_remote_signal_preserves_llm_remote_false():
    """RECONCILE PRECEDENCE: when the text carries NO remote or office signals
    (h_remote is None), reconcile must NOT override the LLM's correct remote=False.

    This tests the no-over-correction invariant: if the LLM read 'office' from
    context that the heuristic cannot see (e.g. prose description), the heuristic
    produces h_remote=None and the LLM value is preserved unchanged.
    """
    # Office-only job with no recognizable remote/office keywords in the text.
    llm_office = _from_dict({
        "title": "Python Dev",
        "source_channel": "@ch",
        "stack": ["python"],
        "remote": False,   # LLM correctly read office-only from context
        "contact": None,
        "benefits": [],
    })
    # Text deliberately has neither remote nor office keywords.
    text = "Python разработчик, Москва. Оклад 300к. Контакт @hr"
    out = _extract_mod.reconcile(llm_office, text, "@ch", None)
    assert out.remote is False, (
        f"reconcile must preserve LLM remote=False when no heuristic signal; got {out.remote}"
    )


def test_reconcile_no_remote_signal_preserves_llm_remote_true():
    """RECONCILE PRECEDENCE (symmetric): when h_remote is None, a correct LLM
    remote=True is also left untouched."""
    llm_remote = _from_dict({
        "title": "Remote ML Eng",
        "source_channel": "@ch",
        "stack": ["python"],
        "remote": True,   # LLM detected remote from some cue the heuristic misses
        "contact": None,
        "benefits": [],
    })
    # Text with no standard remote/office keywords.
    text = "ML Engineer. DM for details. @ml_hr"
    out = _extract_mod.reconcile(llm_remote, text, "@ch", None)
    assert out.remote is True, (
        f"reconcile must preserve LLM remote=True when no heuristic signal; got {out.remote}"
    )


def test_reconcile_real_body_email_from_llm_not_stripped():
    """CHANNEL-LEAK GUARD: reconcile must NOT strip a real recruiter email that
    the LLM correctly extracted from the post body.

    The guard only drops a contact that equals the SOURCE CHANNEL. A genuine
    email address from the body is neither the channel nor the source link, so
    it must survive the reconcile pass unchanged.
    """
    channel = "ai_rabota"
    link = "https://t.me/ai_rabota/999"
    llm_good_contact = _from_dict({
        "title": "ML Engineer",
        "source_channel": channel,
        "stack": ["python"],
        "contact": "hr@realcompany.com",   # real body email, correctly extracted
        "contact_type": None,
        "benefits": [],
        "source_link": link,
    })
    text = "Remote ML Engineer. Contacts: hr@realcompany.com"
    out = _extract_mod.reconcile(llm_good_contact, text, channel, link)
    # Real email must be preserved (not stripped as a channel-leak).
    assert out.contact == "hr@realcompany.com", (
        f"Real body email must survive reconcile; got {out.contact!r}"
    )


# ---------------------------------------------------------------------------
# ingest_web parser: long post — bottom line captured (point 6)
# ---------------------------------------------------------------------------

from job_hunter import ingest_web as _web_mod


def test_ingest_web_parser_captures_bottom_of_long_post():
    """The ingest_web HTML parser must capture the FULL message body with no
    length cap. A contact on the very last line of a long post must appear in
    IngestMessage.raw_text — confirming no truncation occurs.

    This reproduces the netbell scenario: «Контакты: info@netbell.ru» appears
    at the very bottom of a multi-section vacancy, and the parser must deliver
    the complete text so reconcile/heuristic can detect the contact.
    """
    # Build a long HTML message (~110+ lines) with the contact on the last line.
    long_body_lines = [
        "Вакансия: ML Engineer",
        "#УдаленкаРФ #middle",
        "Компания: ТестКомпани",
        "",
        "🔹 Что предстоит делать:",
        *[f"- Задача {i}" for i in range(1, 40)],   # 39 task lines
        "",
        "🔹 Что важно:",
        *[f"- Требование {i}" for i in range(1, 20)],  # 19 requirement lines
        "",
        "🔹 Что мы предлагаем:",
        "- Полная удалёнка",
        "- ДМС",
        "- Обучение",
        "",
        "Контакты: info@testcompany.ru",   # BOTTOM LINE — must be captured
    ]
    inner_html = "<br>".join(long_body_lines)
    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="testchan/42">'
        '<div class="tgme_widget_message_text js-message_text">'
        + inner_html
        + "</div></div>"
    )
    msgs = _web_mod.parse_channel_html(html, "@testchan")
    assert len(msgs) == 1, f"Expected 1 message, got {len(msgs)}"
    raw = msgs[0].raw_text
    # The bottom-line contact must be present in the captured text.
    assert "info@testcompany.ru" in raw, (
        "Bottom-line contact not found in parsed text — parser may be truncating. "
        f"Last 200 chars of raw_text: {raw[-200:]!r}"
    )
    # The top of the post must also be present (sanity).
    assert "ML Engineer" in raw, "Top of post missing from raw_text"
    assert "Компания: ТестКомпани" in raw, "Company line missing from raw_text"


# ---------------------------------------------------------------------------
# 5th pass: remote-resolution correctness bugs (directional reconcile +
# negated-remote false positives in _detect_remote). Pure functions; no mocks.
# ---------------------------------------------------------------------------


def _llm_remote(value, **extra):
    """Build a minimal ExtractResult with a given LLM-read remote value."""
    base = {
        "title": "Python Dev",
        "source_channel": "@ch",
        "stack": ["python"],
        "remote": value,
        "contact": None,
        "benefits": [],
    }
    base.update(extra)
    return _from_dict(base)


# --- BUG 1: reconcile remote precedence is directional (upgrade only) --------


def test_reconcile_remote_true_preserved_when_no_positive_signal():
    """h_remote is None (no remote/office keywords): a definite LLM remote=True
    must be left untouched (the heuristic never downgrades)."""
    out = _extract_mod.reconcile(
        _llm_remote(True), "Python разработчик, Москва. Контакт @hr", "@ch", None
    )
    assert out.remote is True, f"expected True preserved, got {out.remote}"


def test_reconcile_remote_true_not_downgraded_by_office_heuristic():
    """The heuristic reads office-only (h_remote is False) but the LLM already
    gave a DEFINITE remote=True; reconcile must NOT downgrade it to False."""
    text = "Работа в офисе, on-site, в офисе ждём вас"
    # sanity: the heuristic really does see office-only here.
    assert _extract_mod._detect_remote(text)[0] is False
    out = _extract_mod.reconcile(_llm_remote(True), text, "@ch", None)
    assert out.remote is True, (
        f"reconcile must NOT downgrade a definite LLM True to office; got {out.remote}"
    )


def test_reconcile_fills_null_llm_remote_true_on_positive_signal():
    """LLM left remote null; a positive body signal fills it -> True."""
    out = _extract_mod.reconcile(_llm_remote(None), "Полная удалёнка", "@ch", None)
    assert out.remote is True, f"expected fill to True, got {out.remote}"


def test_reconcile_fills_null_llm_remote_false_on_office_signal():
    """LLM left remote null; only an office signal exists -> filled with False."""
    out = _extract_mod.reconcile(
        _llm_remote(None), "Только офис, on-site", "@ch", None
    )
    assert out.remote is False, f"expected fill to False, got {out.remote}"


def test_reconcile_remote_upgraded_by_hashtag_netbell_case():
    """NETBELL: a #УдаленкаРФ hashtag is a positive remote signal and WINS,
    upgrading even a (wrong) LLM remote=False to True."""
    out = _extract_mod.reconcile(
        _llm_remote(False), "Вакансия Python\n#УдаленкаРФ #middle", "@ch", None
    )
    assert out.remote is True, (
        f"#УдаленкаРФ must upgrade remote to True; got {out.remote}"
    )


# --- BUG 2: _detect_remote must not fire on a NEGATED remote phrase ----------


def test_detect_remote_negated_ru_phrase_not_true():
    """'Без удалёнки' ('without remote') must NOT yield remote=True."""
    remote, _ = _extract_mod._detect_remote("Без удалёнки, только офис")
    assert remote is not True, f"negated 'без удалёнки' should not be True; got {remote}"
    # With the office cue present, this resolves to office-only (False).
    assert remote is False, f"expected office/False, got {remote}"


def test_detect_remote_negated_ne_phrase_not_true():
    """'не удалённо' ('not remotely') must NOT yield remote=True."""
    remote, _ = _extract_mod._detect_remote("не удалённо")
    assert remote is not True, f"negated 'не удалённо' should not be True; got {remote}"


def test_detect_remote_negated_no_remote_en_not_true():
    """'office only, no remote' must NOT yield remote=True."""
    remote, _ = _extract_mod._detect_remote("office only, no remote")
    assert remote is not True, f"negated 'no remote' should not be True; got {remote}"
    assert remote is False, f"expected office/False, got {remote}"


def test_detect_remote_positive_ru_phrase_true():
    """A genuine 'Полная удалёнка' still resolves to True."""
    remote, _ = _extract_mod._detect_remote("Полная удалёнка")
    assert remote is True, f"'Полная удалёнка' must be True; got {remote}"


def test_detect_remote_positive_en_phrase_true():
    """A genuine 'Remote-first' still resolves to True."""
    remote, _ = _extract_mod._detect_remote("Remote-first culture")
    assert remote is True, f"'Remote-first' must be True; got {remote}"


def test_detect_remote_mixed_negated_and_positive_is_true():
    """A negated phrase plus a separate POSITIVE phrase still yields True
    (one legitimate remote signal is enough)."""
    remote, _ = _extract_mod._detect_remote("Без удалёнки. Шутка — полная удалёнка")
    assert remote is True, f"a positive signal must win; got {remote}"


# ---------------------------------------------------------------------------
# 6th pass: two specific upgrade-only / negation-guard edge cases
# requested by the re-validation task.
# ---------------------------------------------------------------------------


def test_reconcile_null_llm_remote_stays_none_when_negated_remote_and_no_office():
    """Upgrade-only precedence: when the ONLY remote-related text is a NEGATED
    phrase ('Без удалёнки') with NO office keyword, the heuristic resolves to
    h_remote=None (unknown -- the negation consumed the signal but there is no
    positive office indicator either).  A null LLM remote must stay null in
    this case; it must NOT be filled with False.

    This validates the fill rule: null->False only fires on an OFFICE-ONLY
    signal (h_remote is False), not when h_remote is None after negation.
    """
    llm_null = _from_dict(dict(
        title="t", source_channel="@c", stack=[], remote=None, contact=None, benefits=[]
    ))
    # 'Без удалёнки.' has no office keyword; after negation h_remote=None.
    out = _extract_mod.reconcile(llm_null, "Без удалёнки.", "@c", None)
    assert out.remote is None, (
        f"negated-remote + no-office -> h_remote=None -> LLM null must stay None; got {out.remote}"
    )


def test_reconcile_null_llm_remote_fills_false_with_office_plus_negated_remote():
    """Upgrade-only precedence: when the text has BOTH a negated remote phrase
    AND a positive office keyword, _detect_remote resolves to (False, False)
    (h_remote=False: office only).  A null LLM remote must then be FILLED with
    False (the null->fill rule for h_remote is not None).
    """
    llm_null = _from_dict(dict(
        title="t", source_channel="@c", stack=[], remote=None, contact=None, benefits=[]
    ))
    out = _extract_mod.reconcile(llm_null, "Без удалёнки. В офисе.", "@c", None)
    assert out.remote is False, (
        f"negated-remote + office -> h_remote=False -> LLM null filled with False; got {out.remote}"
    )


def test_detect_remote_negator_in_unrelated_earlier_clause_does_not_suppress():
    """Negation guard must NOT over-suppress when a negator word appears in an
    EARLIER, unrelated clause and a legitimate remote phrase follows later.

    The guard uses _REMOTE_NEGATOR_RE with a \$ anchor so it only fires when
    the negator is IMMEDIATELY before the remote token (allowing only
    whitespace/punctuation separators). A negator elsewhere in the text must
    not reach forward to suppress a remote match in a later clause.

    Tested patterns:
      - 'No formal degree required. Remote position.' -> True
        ('no' + long prefix ending in '. ' -> negator NOT immediately before 'Remote')
      - 'We do not offer a gym, but remote work is available.' -> True
        ('not' does not match the negator set; 'not' is not in the RE)
      - 'Не требуем опыта. Удалённая работа.' -> True
        ('не' is before 'требуем', not immediately before 'Удалённая')
    """
    cases = [
        ("No formal degree required. Remote position.", True),
        ("We do not offer a gym, but remote work is available.", True),
        ("Не требуем опыта. Удалённая работа.", True),
        ("Без визы не пройдёшь. Удалённая работа.", True),
    ]
    for text, expected_is_remote in cases:
        result, _ = _extract_mod._detect_remote(text)
        assert result is True, (
            f"negator in unrelated clause must not suppress remote; "
            f"text={text!r}, expected True, got {result}"
        )


# ---------------------------------------------------------------------------
# Draft delivery rework: manual-confirm UX + deterministic links + copy button
# ---------------------------------------------------------------------------


def test_agents_draft_appends_deterministic_signature(fake_llm):
    """agents.draft must append the GitHub link + резюме placeholder verbatim
    after the LLM body, and must NOT fabricate any http resume URL."""
    from job_hunter import agents
    from job_hunter.schema_extract import ExtractResult

    fake_llm.set_for("application message", "Здравствуйте, мне интересна позиция.")
    ex = ExtractResult(title="AI Eng", company="Нетбелл", source_channel="@c")
    out = agents.draft(fake_llm, ex, "raw vacancy text")

    assert out.startswith("Здравствуйте, мне интересна позиция.")
    # Signature comes from the (example) profile -> generic placeholders.
    assert "github.com/example" in out
    assert "[resume: link]" in out
    # No fabricated resume http(s) URL — only the literal placeholder.
    assert "http" not in out.split("[resume")[0].split("github.com")[1]


def test_append_draft_signature_idempotent_and_empty():
    from job_hunter import agents

    once = agents.append_draft_signature("Body.")
    twice = agents.append_draft_signature(once)
    assert once == twice
    assert once.count("github.com/example") == 1
    # Empty body still yields the signature (links always present).
    empty = agents.append_draft_signature("")
    assert "github.com/example" in empty and "[resume: link]" in empty


def test_notify_draft_attaches_manual_confirm_and_copy_contact(conn):
    """notify_draft sends plain text (copy-friendly) with the «✅ Отправила»
    manual-confirm button and, when a short contact exists, a «📋 Контакт»
    copy_text button that serialises correctly."""
    import asyncio as _a

    from job_hunter.config import Config
    from job_hunter.pipeline import Deps
    from job_hunter.states import DRAFTED

    item_id = store.insert_item(conn, raw_text="x", source_channel="@c",
                                source_message_id="cb1")
    ex = {"title": "AI Eng", "draft": "Отклик текст\n\nGitHub: github.com/example\n[resume: link]",
          "contact": "hr@example.com", "contact_type": None}
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex))
    store.update_state(conn, item_id, DRAFTED, from_state="extracted",
                       kind="deterministic", actor="system")

    class _CaptureBot:
        def __init__(self):
            self.calls = []

        async def send_message(self, chat_id, text, **kwargs):
            self.calls.append({"chat_id": chat_id, "text": text, **kwargs})

    cfg = Config(bot_token="x", notify_chat_id=123, allowed_user_ids={777})
    b = bot.JobHunterBot(cfg, conn, Deps(llm_client=None, fx=None))
    b._bot = _CaptureBot()
    b._ensure = lambda: None  # type: ignore[assignment]

    _a.run(b.notify_draft(item_id))

    assert len(b._bot.calls) == 1
    call = b._bot.calls[0]
    # Plain text, copy-friendly: no HTML parse_mode that could corrupt it.
    assert call.get("parse_mode") is None
    assert "github.com/example" in call["text"]
    assert "[resume: link]" in call["text"]
    # Keyboard: manual-confirm + copy contact.
    kb = call["reply_markup"]
    dumped = kb.model_dump_json(exclude_none=True)
    assert '"callback_data":"' + bot.encode_callback("send", item_id) + '"' in dumped
    assert '"copy_text":{"text":"hr@example.com"}' in dumped


def test_notify_draft_omits_copy_button_when_no_contact(conn):
    import asyncio as _a

    from job_hunter.config import Config
    from job_hunter.pipeline import Deps
    from job_hunter.states import DRAFTED

    item_id = store.insert_item(conn, raw_text="x", source_channel="@c",
                                source_message_id="cb2")
    ex = {"title": "AI Eng", "draft": "Текст", "contact": None}
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex))
    store.update_state(conn, item_id, DRAFTED, from_state="extracted",
                       kind="deterministic", actor="system")

    class _CaptureBot:
        def __init__(self):
            self.calls = []

        async def send_message(self, chat_id, text, **kwargs):
            self.calls.append({"chat_id": chat_id, "text": text, **kwargs})

    cfg = Config(bot_token="x", notify_chat_id=123, allowed_user_ids={777})
    b = bot.JobHunterBot(cfg, conn, Deps(llm_client=None, fx=None))
    b._bot = _CaptureBot()
    b._ensure = lambda: None  # type: ignore[assignment]

    _a.run(b.notify_draft(item_id))
    kb = b._bot.calls[0]["reply_markup"]
    dumped = kb.model_dump_json(exclude_none=True)
    assert "copy_text" not in dumped  # no contact -> no copy button


def test_non_allowed_user_send_callback_forbidden(conn, fake_llm, fake_fx):
    """A non-allowlisted user tapping «✅ Отправила» on a DRAFTED item must be
    forbidden: advance NOT called, no state change."""
    import asyncio as _a

    from job_hunter.config import Config
    from job_hunter.states import DRAFTED

    fake_llm.set_for("hiring-fit JUDGE",
                     '{"relevance_score": 85, "Обоснование": "ok"}')
    fake_llm.set_for("research", '{"summary":"s","talking_points":[],"questions":[]}')
    fake_llm.set_for("application message", "Отклик.")
    deps_full = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)

    item_id = _insert(conn, GOOD_POST, mid="forbid_send")
    pipeline.run_to_gate(conn, item_id, deps=deps_full)
    pipeline.advance_by_id(conn, item_id, decision=DECISION_APPROVE, deps=deps_full)
    pipeline.advance_by_id(conn, item_id, deps=deps_full)  # research
    pipeline.advance_by_id(conn, item_id, deps=deps_full)  # draft
    assert store.get_item(conn, item_id).state == DRAFTED

    cfg = Config(bot_token="x", notify_chat_id=123, allowed_user_ids={777})
    b = bot.JobHunterBot(cfg, conn, deps_full)

    calls = {"advance": 0}
    orig = pipeline.advance_by_id

    def _spy(*a, **k):
        calls["advance"] += 1
        return orig(*a, **k)

    pipeline.advance_by_id = _spy  # type: ignore[assignment]
    try:
        class _U:
            id = 999  # NOT in the allowlist

        class _M:
            async def edit_reply_markup(self, reply_markup=None):
                pass

        class _Cb:
            data = bot.encode_callback("send", item_id)
            from_user = _U()
            message = _M()
            answer_calls = 0
            answered = None

            async def answer(self, text=None):
                self.answer_calls += 1
                self.answered = text

        cb = _Cb()
        status = _a.run(b.handle_callback(cb))
    finally:
        pipeline.advance_by_id = orig  # type: ignore[assignment]

    assert status == "forbidden"
    assert calls["advance"] == 0
    assert store.get_item(conn, item_id).state == DRAFTED  # unchanged
    assert cb.answered is None


# ---------------------------------------------------------------------------
# 7th pass: Batch A + B specific gaps identified by re-validation
# ---------------------------------------------------------------------------

# --- Batch A / Point 1: company=None path — agents.draft prompt has no "None" ---

def test_agents_draft_company_null_prompt_contains_no_none_word(fake_llm):
    """When company is None, the USER-MESSAGE prompt that agents.draft passes to the
    LLM must NOT contain 'address the отклик to it): None'.  That string would cause
    the LLM to literally write 'None' in the body because it looks like a command.

    This tests the WIRING from agents.draft -> llm.llm_draft -> build_draft_prompt
    as a single chain, whereas test_build_draft_prompt_company_null_no_none_literal
    in test_llm.py only tests the pure build function in isolation.
    """
    from job_hunter import agents
    from job_hunter.schema_extract import ExtractResult

    fake_llm.set_for("application message", "Short application.")
    ex = ExtractResult(title="ML Engineer", company=None, source_channel="@c")

    agents.draft(fake_llm, ex, "vacancy raw text")

    assert fake_llm.calls, "LLM must have been called for the draft"
    user_msg = fake_llm.calls[-1]["user"]
    # The dangerous string that would make the LLM write "None":
    assert "address the отклик to it): None" not in user_msg, (
        "PROMPT contains 'TARGET COMPANY (address the отклик to it): None' which "
        "causes the LLM to emit the literal word 'None' in the draft body"
    )
    # The correct neutral instruction must be present instead:
    assert "neutral opening" in user_msg.lower()


# --- Batch A / Point 1: _SIGNATURE_BLOCK constant contains exact expected strings ---

def test_signature_block_constants_are_exact_and_no_http_resume():
    """agents._SIGNATURE_BLOCK must contain EXACTLY the github.com link and the
    literal резюме placeholder, and MUST NOT contain a fabricated http(s) resume URL.

    This tests the constants directly so a Developer change to the constant text
    that would break the spec (e.g. adding an https:// in front of github.com, or
    fabricating a resume URL) is caught immediately.
    """
    from job_hunter import agents

    # The module constants are rendered from the GENERIC example profile (no
    # personal data); the live signature comes from the loaded profile.
    assert "github.com/example" in agents._SIGNATURE_BLOCK
    assert "[resume: link]" in agents._SIGNATURE_BLOCK
    # Must NOT contain an invented resume URL
    # (a fabricated URL would look like "https://..." or "http://..." before [resume:)
    part_before_resume = agents._SIGNATURE_BLOCK.split("[resume:")[0]
    # The only URL-like string in the signature must be the GitHub link (no https://)
    assert "https://" not in part_before_resume, (
        f"Fabricated https:// URL found before [resume: placeholder in signature block: "
        f"{part_before_resume!r}"
    )
    # GITHUB_LINK and RESUME_PLACEHOLDER constants are coherent (generic example)
    assert agents.GITHUB_LINK == "github.com/example"
    assert agents.RESUME_PLACEHOLDER == "[resume: link]"


# --- Batch B / Point 6: llm_score sends vacancy text in USER, not in cached system ---

def test_llm_score_user_message_contains_raw_text(fake_llm):
    """The per-item VARIABLE vacancy text must be in the USER message (not in any system
    block) so the cached system prefix is byte-identical across ALL items in a run.

    This is the key invariant for prompt caching: the cached prefix = the constant
    system prompt only; the variable part must go in the user turn.
    """
    from job_hunter import llm
    from job_hunter.schema_extract import ExtractResult

    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 70, "Обоснование": "ok"}')
    ex = ExtractResult(title="t", source_channel="@c")
    vacancy_marker = "UNIQUE_VACANCY_RAW_TEXT_MARKER_BATCH_B"
    llm.llm_score(fake_llm, ex, f"{vacancy_marker} job description here")

    call = fake_llm.calls[0]
    # Vacancy text is in the USER message
    assert vacancy_marker in call["user"], "Vacancy text must be in user message"
    # Vacancy text is NOT in the cached system block
    if isinstance(call["system_param"], list):
        for block in call["system_param"]:
            assert vacancy_marker not in block.get("text", ""), (
                "Variable vacancy text must NOT appear in the cached system block"
            )
