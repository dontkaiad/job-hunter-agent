"""advance() over deterministic + HITL + agent paths (in-memory sqlite, mocks)."""

import json

from _llm_extract import llm_extracts_salary_of
from job_hunter import pipeline, store
from job_hunter.pipeline import Deps
from job_hunter.states import (
    APPROVED, BACKLOG, CLOSED, DRAFTED, EXTRACTED, REJECTED, RESEARCHED,
    SCORED, SENT, SKIPPED, SURFACED, SCREENING, INTERVIEW, OFFER, DECLINED,
    DECISION_APPROVE, DECISION_SEND, DECISION_SKIP, DECISION_BACKLOG,
    DECISION_SCREENING, DECISION_INTERVIEW, DECISION_OFFER, DECISION_DECLINE,
    DECISION_CLOSE,
)


def _insert(conn, text, mid="1"):
    return store.insert_item(conn, raw_text=text, source_channel="@c", source_message_id=mid)


GOOD_POST = ("Remote Senior Python engineer building LLM RAG agents with Claude API and "
             "FastAPI. Salary 250000-350000 RUB. Contact @hr_acme")
# Salary below the example profile floor (EUR 1000 * 100 = 100_000 RUB via fake_fx).
LOW_PAY_POST = "Python intern, office Moscow, salary 50000 RUB. @hr"


def test_extract_then_score_then_surface(conn, deps):
    item_id = _insert(conn, GOOD_POST)
    r1 = pipeline.advance_by_id(conn, item_id, deps=deps)
    assert r1.to_state == EXTRACTED

    r2 = pipeline.advance_by_id(conn, item_id, deps=deps)
    assert r2.to_state == SCORED

    r3 = pipeline.advance_by_id(conn, item_id, deps=deps)
    assert r3.to_state == SURFACED


def test_hard_reject_low_pay(conn, deps):
    item_id = _insert(conn, LOW_PAY_POST)
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == REJECTED
    trans = store.list_transitions(conn, item_id)
    assert any("hard reject" in (t["reason"] or "").lower() for t in trans)


def test_run_to_gate_stops_at_surface(conn, deps):
    item_id = _insert(conn, GOOD_POST)
    results = pipeline.run_to_gate(conn, item_id, deps=deps)
    assert results[-1].status == "needs_human"
    item = store.get_item(conn, item_id)
    assert item.state == SURFACED


def test_surfaced_needs_human_without_decision(conn, deps):
    item_id = _insert(conn, GOOD_POST)
    pipeline.run_to_gate(conn, item_id, deps=deps)
    res = pipeline.advance_by_id(conn, item_id, deps=deps)
    assert res.status == "needs_human"
    assert "approve" in res.extra["options"]


def test_hitl_skip(conn, deps):
    item_id = _insert(conn, GOOD_POST)
    pipeline.run_to_gate(conn, item_id, deps=deps)
    res = pipeline.advance_by_id(conn, item_id, decision=DECISION_SKIP, deps=deps)
    assert res.to_state == SKIPPED
    # terminal: advancing again is a no-op
    res2 = pipeline.advance_by_id(conn, item_id, decision=DECISION_APPROVE, deps=deps)
    assert res2.status == "terminal"


def test_hitl_backlog_then_promote(conn, deps):
    item_id = _insert(conn, GOOD_POST)
    pipeline.run_to_gate(conn, item_id, deps=deps)
    assert pipeline.advance_by_id(conn, item_id, decision=DECISION_BACKLOG, deps=deps).to_state == BACKLOG
    assert pipeline.advance_by_id(conn, item_id, decision=DECISION_APPROVE, deps=deps).to_state == APPROVED


def test_illegal_decision_is_noop(conn, deps):
    item_id = _insert(conn, GOOD_POST)
    pipeline.run_to_gate(conn, item_id, deps=deps)
    res = pipeline.advance_by_id(conn, item_id, decision=DECISION_SEND, deps=deps)
    assert res.status == "noop"


def test_full_path_approve_research_draft_send_close(conn, deps, fake_llm):
    fake_llm.set_for("research", '{"summary":"s","talking_points":[],"questions":[]}')
    fake_llm.set_for("application message", "Hello, I'd love to apply.")

    item_id = _insert(conn, GOOD_POST)
    pipeline.run_to_gate(conn, item_id, deps=deps)
    # approve -> approved
    assert pipeline.advance_by_id(conn, item_id, decision=DECISION_APPROVE, deps=deps).to_state == APPROVED
    # agent: research
    assert pipeline.advance_by_id(conn, item_id, deps=deps).to_state == RESEARCHED
    # agent: draft
    assert pipeline.advance_by_id(conn, item_id, deps=deps).to_state == DRAFTED

    item = store.get_item(conn, item_id)
    data = json.loads(item.extracted_json)
    # LLM body is preserved; the deterministic GitHub + резюме signature is
    # appended so the literal links are always present for the operator.
    assert data["draft"].startswith("Hello,\nI'm applying for")        # fixed greeting + intro lines
    assert "I'd love to apply." in data["draft"]                          # the model body follows
    assert "github.com/example" in data["draft"]
    assert "[resume: link]" in data["draft"]
    assert "research" in data

    # hitl: send
    assert pipeline.advance_by_id(conn, item_id, decision=DECISION_SEND, deps=deps).to_state == SENT
    # NO auto-close any more: a decision-less advance() on SENT waits for a human.
    waiting = pipeline.advance_by_id(conn, item_id, deps=deps)
    assert waiting.status == "needs_human"
    assert store.get_item(conn, item_id).state == SENT
    # hitl funnel: close it manually -> closed.
    assert pipeline.advance_by_id(conn, item_id, decision=DECISION_CLOSE, deps=deps).to_state == CLOSED


def _drive_to_sent(conn, deps, fake_llm):
    """Helper: take a fresh GOOD_POST item all the way to SENT."""
    fake_llm.set_for("research", '{"summary":"s","talking_points":[],"questions":[]}')
    fake_llm.set_for("application message", "Hello.")
    item_id = _insert(conn, GOOD_POST, mid=str(_drive_to_sent.n))
    _drive_to_sent.n += 1
    pipeline.run_to_gate(conn, item_id, deps=deps)
    pipeline.advance_by_id(conn, item_id, decision=DECISION_APPROVE, deps=deps)
    pipeline.run_to_gate(conn, item_id, deps=deps)  # research + draft
    assert pipeline.advance_by_id(conn, item_id, decision=DECISION_SEND, deps=deps).to_state == SENT
    return item_id


_drive_to_sent.n = 100


def test_funnel_screening_interview_offer(conn, deps, fake_llm):
    item_id = _drive_to_sent(conn, deps, fake_llm)
    assert pipeline.advance_by_id(conn, item_id, decision=DECISION_SCREENING, deps=deps).to_state == SCREENING
    assert pipeline.advance_by_id(conn, item_id, decision=DECISION_INTERVIEW, deps=deps).to_state == INTERVIEW
    assert pipeline.advance_by_id(conn, item_id, decision=DECISION_OFFER, deps=deps).to_state == OFFER
    # offer is terminal: further advance is a no-op 'terminal'.
    assert pipeline.advance_by_id(conn, item_id, deps=deps).status == "terminal"


def test_funnel_decline_from_sent_only_allows_reapprove(conn, deps, fake_llm):
    item_id = _drive_to_sent(conn, deps, fake_llm)
    res = pipeline.advance_by_id(conn, item_id, decision=DECISION_DECLINE, deps=deps)
    assert res.to_state == DECLINED
    # declined != the scoring REJECTED. Since T25 it is re-approvable, so it is
    # not terminal -- but funnel decisions like 'close' are illegal from it.
    assert DECLINED != REJECTED
    assert pipeline.advance_by_id(conn, item_id, decision=DECISION_CLOSE, deps=deps).status == "noop"
    back = pipeline.advance_by_id(conn, item_id, decision=DECISION_APPROVE, deps=deps)
    assert back.to_state == APPROVED


def test_funnel_offer_illegal_before_interview(conn, deps, fake_llm):
    item_id = _drive_to_sent(conn, deps, fake_llm)
    # offer from sent is not a legal decision -> no-op, state unchanged.
    res = pipeline.advance_by_id(conn, item_id, decision=DECISION_OFFER, deps=deps)
    assert res.status == "noop"
    assert store.get_item(conn, item_id).state == SENT


def test_do_draft_passes_full_raw_text_to_llm(conn, deps, fake_llm):
    """FIX 4: the draft step must thread the COMPLETE vacancy raw_text into the
    draft LLM call, so the model can avoid re-asking already-answered details."""
    fake_llm.set_for("research", '{"summary":"s","talking_points":[],"questions":[]}')
    fake_llm.set_for("application message", "Здравствуйте, мне интересно.")

    raw = GOOD_POST + "\nUNIQUE_RAWTEXT_MARKER_T11 at the very bottom of the post."
    item_id = _insert(conn, raw)
    pipeline.run_to_gate(conn, item_id, deps=deps)
    pipeline.advance_by_id(conn, item_id, decision=DECISION_APPROVE, deps=deps)
    pipeline.advance_by_id(conn, item_id, deps=deps)  # research
    pipeline.advance_by_id(conn, item_id, deps=deps)  # draft

    draft_calls = [c for c in fake_llm.calls if "application message" in c["system"]]
    assert draft_calls, "draft LLM call not made"
    assert "UNIQUE_RAWTEXT_MARKER_T11" in draft_calls[-1]["user"]


def test_score_stored_with_reasoning(conn, fake_llm, fake_fx):
    """T2 stores the relevance_score AND the Обоснование rationale.

    Score above the corridor: ONE Haiku call; its score and text are final (the
    Sonnet refine pass was removed in b2f6a09).
    """
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.responses = [
        '{"relevance_score": 82, "Обоснование": "applied-LLM роль, remote"}',
    ]
    item_id = _insert(conn, GOOD_POST)
    pipeline.advance_by_id(conn, item_id, deps=deps)  # extract
    res = pipeline.advance_by_id(conn, item_id, deps=deps)  # score
    assert res.to_state == SCORED

    item = store.get_item(conn, item_id)
    assert item.relevance_score == 82.0
    blob = json.loads(item.extracted_json)
    assert blob["Обоснование"] == "applied-LLM роль, remote"  # Haiku's text is final


def test_score_routes_haiku_only_below_threshold(conn, fake_llm, fake_fx):
    """Score < 60: only the cheap (Haiku) model is called — Sonnet is NOT invoked."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    # Score 30 < corridor_lo (40) → no Sonnet refinement.
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 30, "Обоснование": "weak fit"}')
    item_id = _insert(conn, GOOD_POST)
    pipeline.advance_by_id(conn, item_id, deps=deps)  # extract
    pipeline.advance_by_id(conn, item_id, deps=deps)  # score
    score_calls = [c for c in fake_llm.calls if "hiring-fit JUDGE" in c["system"]]
    # Exactly ONE call — Haiku only; Sonnet was not invoked.
    assert len(score_calls) == 1, f"expected 1 score call, got {len(score_calls)}"
    assert score_calls[0]["model"] == deps.cheap_model


def test_score_routes_haiku_only_above_corridor(conn, fake_llm, fake_fx):
    """Haiku score > corridor_hi (75 > 70): confident surface, Haiku's score
    AND text are both final — no Sonnet call (#303: a Sonnet "refine" pass
    used to run here purely to reword the Обоснование; removed as wasted
    spend on an already-decided score).
    """
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 75, "Обоснование": "haiku: strong fit"}')
    item_id = _insert(conn, GOOD_POST)
    pipeline.advance_by_id(conn, item_id, deps=deps)  # extract
    pipeline.advance_by_id(conn, item_id, deps=deps)  # score

    item = store.get_item(conn, item_id)
    assert item.relevance_score == 75.0
    blob = json.loads(item.extracted_json)
    assert "haiku" in blob["Обоснование"]

    score_calls = [c for c in fake_llm.calls if "hiring-fit JUDGE" in c["system"]]
    assert len(score_calls) == 1, f"expected 1 score call (Haiku only), got {len(score_calls)}"
    assert score_calls[0]["model"] == deps.cheap_model


def test_score_below_corridor_haiku_only(conn, fake_llm, fake_fx):
    """Haiku score < corridor_lo (30 < 40): confident reject zone, judge NOT called.

    Exactly one LLM call (Haiku); stored score is Haiku's.
    """
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 30, "Обоснование": "weak fit"}')
    item_id = _insert(conn, GOOD_POST)
    pipeline.advance_by_id(conn, item_id, deps=deps)  # extract
    pipeline.advance_by_id(conn, item_id, deps=deps)  # score

    item = store.get_item(conn, item_id)
    assert item.relevance_score == 30.0

    score_calls = [c for c in fake_llm.calls if "hiring-fit JUDGE" in c["system"]]
    assert len(score_calls) == 1, f"expected 1 call (Haiku only, below corridor), got {len(score_calls)}"
    assert score_calls[0]["model"] == deps.cheap_model


def test_score_in_corridor_judge_rescore_is_final(conn, fake_llm, fake_fx):
    """Haiku score in [corridor_lo, corridor_hi] (58 ∈ [50,70]): judge re-scores,
    judge's score is stored as FINAL (not Haiku's).
    """
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    # Haiku says 58 (in corridor) → judge re-scores and returns 72.
    fake_llm.responses = [
        '{"relevance_score": 58, "Обоснование": "haiku: borderline"}',
        '{"relevance_score": 72, "Обоснование": "judge: solid fit — 72/100 ✅ LLM role"}',
    ]
    item_id = _insert(conn, GOOD_POST)
    pipeline.advance_by_id(conn, item_id, deps=deps)  # extract
    pipeline.advance_by_id(conn, item_id, deps=deps)  # score

    item = store.get_item(conn, item_id)
    # Judge's score (72) is final, NOT Haiku's (58).
    assert item.relevance_score == 72.0, f"expected judge score 72, got {item.relevance_score}"
    blob = json.loads(item.extracted_json)
    assert "judge" in blob["Обоснование"], "stored Обоснование must be judge's"

    score_calls = [c for c in fake_llm.calls if "hiring-fit JUDGE" in c["system"]]
    assert len(score_calls) == 2, f"expected 2 calls (Haiku + judge re-score), got {len(score_calls)}"
    assert score_calls[0]["model"] == deps.cheap_model, "first call must be Haiku"
    assert score_calls[1]["model"] == deps.judge_model, "second call must be judge"
    # Judge call is a full re-score (no score anchor in user prompt).
    assert "IMPORTANT" not in score_calls[1]["user"], "corridor judge must NOT use score anchor"


def test_score_corridor_judge_failure_fallback(conn, fake_llm, fake_fx):
    """If judge call fails in corridor, fallback to Haiku score — no exception raised."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)

    call_count = [0]

    def _side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return '{"relevance_score": 58, "Обоснование": "haiku: borderline"}'
        raise RuntimeError("judge timeout")

    fake_llm.complete = _side_effect

    item_id = _insert(conn, GOOD_POST)
    pipeline.advance_by_id(conn, item_id, deps=deps)  # extract
    res = pipeline.advance_by_id(conn, item_id, deps=deps)  # score — must not raise

    assert res.to_state == SCORED, "corridor judge failure must NOT crash T2"
    item = store.get_item(conn, item_id)
    # Fallback: Haiku score stored (58), not a crash-default 0.
    assert item.relevance_score == 58.0, f"expected haiku fallback 58, got {item.relevance_score}"


def test_salary_guard_overrides_high_llm_score(conn, fake_llm, fake_fx):
    """A high Sonnet score must NOT save an item whose pay is below the floor."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=True)
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 90, "Обоснование": "great"}')
    # 90k RUB top < 100k floor (example EUR 1000 * 100 RUB/EUR via fake_fx).
    llm_extracts_salary_of(fake_llm, "Remote Python LLM RAG engineer. Salary 80000-90000 RUB. @hr")
    item_id = _insert(conn, "Remote Python LLM RAG engineer. Salary 80000-90000 RUB. @hr")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == REJECTED
    assert item.relevance_score == 90.0  # the high score was stored...
    trans = store.list_transitions(conn, item_id)
    assert any("hard reject" in (t["reason"] or "").lower() for t in trans)


def test_location_guard_overrides_high_llm_score(conn, fake_llm, fake_fx):
    """Office-only vacancy with explicit remote=false + relocation=false must
    hard-reject even with a top relevance score — physically unavailable to
    the candidate (no visa/right to work there without sponsorship)."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=True)
    fake_llm.set_for(
        "strict JSON",
        '{"title":"Senior Python Engineer","stack":["python","llm"],'
        '"remote":false,"relocation":false,"location":"Moscow office"}',
    )
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 95, "Обоснование": "perfect stack"}')
    item_id = _insert(conn, "Office Python Senior LLM role in Moscow. @hr")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == REJECTED
    assert item.relevance_score == 95.0  # the high score was stored...
    trans = store.list_transitions(conn, item_id)
    assert any("hard reject" in (t["reason"] or "").lower() for t in trans)
    assert any("физически недоступно" in (t["reason"] or "") for t in trans)


def test_location_guard_fires_when_relocation_unmentioned(conn, fake_llm, fake_fx):
    """The common real case: an office post that never mentions relocation at
    all extracts as relocation=None (not False) — must still hard-reject."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=True)
    fake_llm.set_for(
        "strict JSON",
        '{"title":"Senior Python Engineer","stack":["python","llm"],'
        '"remote":false,"location":"Moscow office"}',
    )
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 95, "Обоснование": "perfect stack"}')
    item_id = _insert(conn, "Office Python Senior LLM role in Moscow. @hr")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == REJECTED
    trans = store.list_transitions(conn, item_id)
    assert any("физически недоступно" in (t["reason"] or "") for t in trans)


def test_location_guard_not_triggered_with_relocation_support(conn, fake_llm, fake_fx):
    """Office-only IS surfaced when relocation/visa support is confirmed."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=True)
    fake_llm.set_for(
        "strict JSON",
        '{"title":"Senior Python Engineer","stack":["python","llm"],'
        '"remote":false,"relocation":true,"location":"Berlin office"}',
    )
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 90, "Обоснование": "great fit"}')
    item_id = _insert(conn, "Office Python Senior LLM role in Berlin, relocation package provided. @hr")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == SURFACED


def test_location_guard_not_triggered_when_remote(conn, fake_llm, fake_fx):
    """A confirmed remote vacancy is never touched by the location guard."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=True)
    fake_llm.set_for(
        "strict JSON",
        '{"title":"Senior Python Engineer","stack":["python","llm"],'
        '"remote":true,"relocation":false}',
    )
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 90, "Обоснование": "great fit"}')
    item_id = _insert(conn, "Remote Python Senior LLM role. @hr")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == SURFACED


def test_llm_extract_used_when_enabled(conn, fake_llm, fake_fx):
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=True)
    fake_llm.set_for("strict JSON", '{"title":"LLM Eng","stack":["python","llm"],'
                                    '"salary_max":300000,"currency":"RUB","remote":true}')
    item_id = _insert(conn, "some raw text describing a remote LLM engineering role")
    res = pipeline.advance_by_id(conn, item_id, deps=deps)
    assert res.to_state == EXTRACTED
    assert "llm" in res.reason
    item = store.get_item(conn, item_id)
    assert json.loads(item.extracted_json)["title"] == "LLM Eng"
    # Extraction must route to the cheap (Haiku) model.
    extract_calls = [c for c in fake_llm.calls if "strict JSON" in c["system"]]
    assert extract_calls[0]["model"] == deps.cheap_model


def test_llm_extract_error_defers_instead_of_falling_back(conn, fake_fx):
    """Was test_llm_extract_falls_back_on_error: a failing extract used to drop to the
    heuristic extractor silently (2026-11-02..05: 125 vacancies scored from poor fields,
    ~210 deleted). It now DEFERS the vacancy; see tests/test_extract_defer.py."""
    class BoomLLM:
        def complete(self, *a, **k):
            raise RuntimeError("api down")

    deps = Deps(llm_client=BoomLLM(), fx=fake_fx, use_llm_extract=True)
    item_id = _insert(conn, "Remote Python LLM engineer role, apply via @hr contact")
    res = pipeline.advance_by_id(conn, item_id, deps=deps)
    assert res.status == "noop" and "extract deferred" in res.reason
    it = store.get_item(conn, item_id)
    assert it.state == "discovered" and it.extracted_json is None


# --- LLM scoring failure must DEFER the vacancy, never drop it ---------------


class BadRequestError(Exception):
    """Stand-in for anthropic.BadRequestError (matched by class name)."""

    status_code = 400


def _credit_error():
    return BadRequestError(
        "Error code: 400 - Your credit balance is too low to access the Anthropic API."
    )


def _raising(exc):
    def _complete(system, user, **kwargs):
        raise exc

    return _complete


def _extract_only(conn, deps, text=GOOD_POST, mid="1"):
    item_id = _insert(conn, text, mid)
    pipeline.advance_by_id(conn, item_id, deps=deps)  # T1 extract
    assert store.get_item(conn, item_id).state == EXTRACTED
    return item_id


def test_credit_error_defers_and_halts_run_scoring(conn, fake_llm, fake_fx):
    stats = pipeline.ScoreRunStats(halt_on_fatal=True)
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False,
                min_persist_score=25, score_stats=stats)
    a = _extract_only(conn, deps, mid="1")
    b = _extract_only(conn, deps, mid="2")
    fake_llm.complete = _raising(_credit_error())

    res_a = pipeline.advance_by_id(conn, a, deps=deps)
    assert res_a.status == "noop" and "deferred" in res_a.reason
    assert stats.halted

    calls = []
    fake_llm.complete = lambda *a_, **k: calls.append(1) or "{}"
    res_b = pipeline.advance_by_id(conn, b, deps=deps)
    assert res_b.status == "noop" and "halted" in res_b.reason
    assert calls == []  # no LLM call burned after the outage was detected

    for item_id in (a, b):
        it = store.get_item(conn, item_id)
        assert it is not None and it.state == EXTRACTED
        assert it.relevance_score is None
    # An account-wide outage does not burn the vacancy's attempts.
    assert store.get_item(conn, a).score_attempts == 0


def test_parse_error_defers_and_counts_attempt(conn, fake_llm, fake_fx):
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False, min_persist_score=25)
    item_id = _extract_only(conn, deps)
    fake_llm.complete = lambda *a, **k: "sorry, no json here"

    res = pipeline.advance_by_id(conn, item_id, deps=deps)

    assert res.status == "noop"
    it = store.get_item(conn, item_id)
    assert it is not None and it.state == EXTRACTED
    assert it.score_attempts == 1 and not it.score_failed
    assert deps.score_stats.kinds == {pipeline.FAIL_PARSE: 1}


def test_three_failures_mark_score_failed_but_keep_item(conn, fake_llm, fake_fx):
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False,
                min_persist_score=25, score_max_attempts=3)
    item_id = _extract_only(conn, deps)
    fake_llm.complete = lambda *a, **k: "not json"

    for _ in range(3):
        pipeline.advance_by_id(conn, item_id, deps=deps)

    it = store.get_item(conn, item_id)
    assert it is not None and it.state == EXTRACTED
    assert it.score_attempts == 3 and it.score_failed
    assert deps.score_stats.marked_failed == 1
    # Harvest no longer picks it up, and a direct advance does nothing.
    assert store.list_by_state(conn, EXTRACTED, include_score_failed=False) == []
    assert [i.id for i in store.list_by_state(conn, EXTRACTED)] == [item_id]
    fake_llm.complete = _raising(AssertionError("must not be called"))
    assert pipeline.advance_by_id(conn, item_id, deps=deps).status == "noop"


def test_normal_low_score_is_still_deleted(conn, fake_llm, fake_fx):
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False, min_persist_score=25)
    item_id = _insert(conn, GOOD_POST)
    fake_llm.responses = ['{"relevance_score": 10, "Обоснование": "wrong domain"}']

    pipeline.run_to_gate(conn, item_id, deps=deps)

    assert store.get_item(conn, item_id) is None


def test_normal_high_score_is_still_surfaced(conn, fake_llm, fake_fx):
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False, min_persist_score=25)
    item_id = _insert(conn, GOOD_POST)
    fake_llm.responses = ['{"relevance_score": 82, "Обоснование": "strong fit"}']

    results = pipeline.run_to_gate(conn, item_id, deps=deps)

    assert any(r.to_state == SURFACED for r in results)
    assert store.get_item(conn, item_id).state == SURFACED


def test_scoring_alert_text_thresholds():
    from job_hunter.run import scoring_alert_text

    ok = pipeline.ScoreRunStats(attempted=50, failed=5, kinds={pipeline.FAIL_PARSE: 5})
    assert scoring_alert_text(ok) is None
    bad = pipeline.ScoreRunStats(attempted=10, failed=4, deferred=4,
                                 kinds={pipeline.FAIL_PARSE: 4})
    assert "4/10" in scoring_alert_text(bad)
    credits = pipeline.ScoreRunStats(attempted=100, failed=1, deferred=1, halted=True,
                                     skipped_halted=30, kinds={pipeline.FAIL_FATAL: 1})
    text = scoring_alert_text(credits)
    assert "credits/auth" in text and "HALTED" in text and "30" in text


def test_score_prompt_version_is_switchable_per_deps(conn, fake_llm, fake_fx):
    """SCORE_PROMPT_VERSION: v1 (bare Deps default) vs v2 (config default) reach the model."""
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 85, "Обоснование": "good"}')
    for version, expect in (("v1", False), ("v2", True)):
        fake_llm.calls.clear()
        deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False, score_prompt_version=version)
        item_id = _insert(conn, GOOD_POST + f" {version}", mid=version)
        pipeline.advance_by_id(conn, item_id, deps=deps)  # extract
        pipeline.advance_by_id(conn, item_id, deps=deps)  # score
        systems = [c["system"] for c in fake_llm.calls if "hiring-fit JUDGE" in c["system"]]
        assert systems and all(("LOCATION IS NOT SCORED" in s) is expect for s in systems)


def test_config_score_prompt_version_default_and_rollback():
    import pytest
    from job_hunter.config import load_config

    assert load_config({}).score_prompt_version == "v2"
    assert load_config({"SCORE_PROMPT_VERSION": "v1"}).score_prompt_version == "v1"
    with pytest.raises(ValueError):
        load_config({"SCORE_PROMPT_VERSION": "v9"})
