"""advance() over deterministic + HITL + agent paths (in-memory sqlite, mocks)."""

import json

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
    assert data["draft"].startswith("Hello, I'd love to apply.")
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


def test_funnel_decline_from_sent_is_terminal(conn, deps, fake_llm):
    item_id = _drive_to_sent(conn, deps, fake_llm)
    res = pipeline.advance_by_id(conn, item_id, decision=DECISION_DECLINE, deps=deps)
    assert res.to_state == DECLINED
    # declined != the scoring REJECTED; and it is terminal.
    assert DECLINED != REJECTED
    assert pipeline.advance_by_id(conn, item_id, decision=DECISION_CLOSE, deps=deps).status == "terminal"


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


def test_sonnet_score_stored_with_reasoning(conn, fake_llm, fake_fx):
    """T2 stores the Sonnet relevance_score AND the Обоснование rationale."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.set_for(
        "hiring-fit JUDGE",
        '{"relevance_score": 82, "Обоснование": "applied-LLM роль, remote"}',
    )
    item_id = _insert(conn, GOOD_POST)
    pipeline.advance_by_id(conn, item_id, deps=deps)  # extract
    res = pipeline.advance_by_id(conn, item_id, deps=deps)  # score
    assert res.to_state == SCORED

    item = store.get_item(conn, item_id)
    assert item.relevance_score == 82.0
    blob = json.loads(item.extracted_json)
    assert blob["Обоснование"] == "applied-LLM роль, remote"


def test_score_routes_to_judge_model(conn, fake_llm, fake_fx):
    """The relevance score call must request the JUDGE (Sonnet) model id."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 70, "Обоснование": "ok"}')
    item_id = _insert(conn, GOOD_POST)
    pipeline.advance_by_id(conn, item_id, deps=deps)  # extract (heuristic; no LLM call)
    pipeline.advance_by_id(conn, item_id, deps=deps)  # score (Sonnet)
    # Exactly one LLM call happened (the score); it used the judge model.
    score_calls = [c for c in fake_llm.calls if "hiring-fit JUDGE" in c["system"]]
    assert len(score_calls) == 1
    assert score_calls[0]["model"] == deps.judge_model


def test_salary_guard_overrides_high_llm_score(conn, fake_llm, fake_fx):
    """A high Sonnet score must NOT save an item whose pay is below the floor."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 90, "Обоснование": "great"}')
    # 90k RUB top < 100k floor (example EUR 1000 * 100 RUB/EUR via fake_fx).
    item_id = _insert(conn, "Remote Python LLM RAG engineer. Salary 80000-90000 RUB. @hr")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == REJECTED
    assert item.relevance_score == 90.0  # the high score was stored...
    trans = store.list_transitions(conn, item_id)
    assert any("hard reject" in (t["reason"] or "").lower() for t in trans)


def test_llm_extract_used_when_enabled(conn, fake_llm, fake_fx):
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=True)
    fake_llm.set_for("strict JSON", '{"title":"LLM Eng","stack":["python","llm"],'
                                    '"salary_max":300000,"currency":"RUB","remote":true}')
    item_id = _insert(conn, "some raw text")
    res = pipeline.advance_by_id(conn, item_id, deps=deps)
    assert res.to_state == EXTRACTED
    assert "llm" in res.reason
    item = store.get_item(conn, item_id)
    assert json.loads(item.extracted_json)["title"] == "LLM Eng"
    # Extraction must route to the cheap (Haiku) model.
    extract_calls = [c for c in fake_llm.calls if "strict JSON" in c["system"]]
    assert extract_calls[0]["model"] == deps.cheap_model


def test_llm_extract_falls_back_on_error(conn, fake_fx):
    class BoomLLM:
        def complete(self, *a, **k):
            raise RuntimeError("api down")

    deps = Deps(llm_client=BoomLLM(), fx=fake_fx, use_llm_extract=True)
    item_id = _insert(conn, "Remote Python LLM role @hr")
    res = pipeline.advance_by_id(conn, item_id, deps=deps)
    assert res.to_state == EXTRACTED
    assert "fallback" in res.reason
