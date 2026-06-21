"""ORCHESTRATION: the advance() state-machine dispatcher (DESIGN.md §1).

advance() is the SOLE writer of work_items.state. It combines pure decisions
(states / extract / scoring) with store writes inside one transaction, and
drives the real LLM + FX integrations injected via a Deps bundle.

Transition kinds:
  - deterministic: T1 (extract), T2 (score), T3/T4 (reject/surface), T13 (close)
  - hitl:          T5..T9 (gate decisions), T12 (send)
  - agent(LLM):    T10 (research), T11 (draft)

Idempotency: advancing a terminal item is a no-op 'terminal' result.
HITL transitions without a legal decision return 'needs_human' and do not move.
"""

from __future__ import annotations

import json

import psycopg
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import agents, scoring, store
from .extract import extract as heuristic_extract
from .extract import reconcile as reconcile_extract
from .fx import FxRates
from .llm import CHEAP_MODEL, JUDGE_MODEL, LLMClient, llm_extract, llm_score
from .profile import Profile, example_profile
from .schema_extract import ExtractResult, from_dict, serialize
from .scoring import passes_threshold
from .states import (
    APPROVED, DRAFTED, EXTRACTED, KIND_AGENT, KIND_DETERMINISTIC, KIND_HITL,
    REJECTED, RESEARCHED, SCORED, SENT, SURFACED, CLOSED, DISCOVERED,
    allowed_transitions, is_terminal, transition_for_decision,
)
from .store import WorkItem

ACTOR_SYSTEM = "system"
ACTOR_HUMAN = "human"
ACTOR_AGENT = "agent"


@dataclass
class Deps:
    """Injected collaborators. Any may be None for deterministic-only runs.

    - llm_client: enables smart extract, rubric score, research, draft.
    - fx: FxRates for salary RUB conversion (prefilter + display).
    - use_llm_extract: when True and llm_client present, T1 uses the LLM
      (falling back to heuristics on failure). Otherwise heuristics only.
    - cheap_model / judge_model: per-step model routing ids. cheap_model
      (Haiku) is used for extraction/research/draft; judge_model (Sonnet) for
      the relevance score.
    """

    llm_client: Optional[LLMClient] = None
    fx: Optional[FxRates] = None
    use_llm_extract: bool = True
    cheap_model: str = CHEAP_MODEL
    judge_model: str = JUDGE_MODEL
    # Candidate profile (loaded from config/profile.*.yaml). Drives the rubric
    # profile block, the draft gender/honesty/signature, and the salary floor.
    # Defaults to the GENERIC example profile so deterministic-only / test runs
    # work without personal data; live runs inject the loaded local profile.
    profile: Profile = field(default_factory=example_profile)


@dataclass
class AdvanceResult:
    status: str                 # 'moved' | 'needs_human' | 'terminal' | 'noop'
    item_id: int
    from_state: str
    to_state: Optional[str] = None
    transition: Optional[str] = None  # T-name
    reason: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


# --- Helpers ----------------------------------------------------------------


def _load_extracted(item: WorkItem) -> Optional[ExtractResult]:
    if not item.extracted_json:
        return None
    try:
        return from_dict(json.loads(item.extracted_json))
    except Exception:
        return None


def _salary_max_rub(extracted: ExtractResult, deps: Deps) -> Optional[float]:
    """Convert the top of the salary range to RUB, if possible."""
    if extracted.salary_max is None and extracted.salary_min is None:
        return None
    top = extracted.salary_max if extracted.salary_max is not None else extracted.salary_min
    if extracted.currency is None:
        return None
    if extracted.currency.upper() == "RUB":
        return float(top)
    if deps.fx is None:
        return None
    try:
        return deps.fx.convert(top, extracted.currency)
    except Exception:
        return None


def _salary_floor_rub(deps: Deps) -> Optional[float]:
    """Convert the candidate's EUR/month-gross salary floor to RUB via FX.

    The floor value comes from the loaded profile (``deps.profile.
    salary_floor_eur``), NOT a hardcoded constant. Mirrors ``_salary_max_rub``:
    both the salary top and this floor are taken to a common currency (RUB) so
    the deterministic guard in scoring.py can compare them regardless of the
    posting's currency. Returns None when FX is unavailable, in which case the
    guard simply cannot fire.
    """
    if deps.fx is None:
        return None
    try:
        return deps.fx.convert(deps.profile.salary_floor_eur, "EUR")
    except Exception:
        return None


# --- Per-transition handlers ------------------------------------------------


def _do_extract(conn: psycopg.Connection, item: WorkItem, deps: Deps) -> AdvanceResult:
    """T1: discovered -> extracted (LLM smart extract, heuristic fallback)."""
    source_channel = item.source_channel or ""
    raw = item.raw_text or ""
    extracted: ExtractResult
    used = "heuristic"

    if deps.use_llm_extract and deps.llm_client is not None:
        try:
            extracted = llm_extract(
                deps.llm_client, raw, source_channel, item.source_link,
                model=deps.cheap_model,
            )
            # INTEGRATION FIX: the LLM extract alone misses deterministic signals
            # (hashtag remote/seniority, "Компания: X", benefits, bottom contact).
            # Run a deterministic RECONCILE pass over the SAME raw_text so those
            # fields always reach the ExtractResult, even when Haiku omits them.
            # Precedence per field is documented in extract.reconcile.
            extracted = reconcile_extract(
                extracted, raw, source_channel, item.source_link
            )
            used = "llm+reconcile"
        except Exception:
            extracted = heuristic_extract(raw, source_channel, item.source_link)
            used = "heuristic(fallback)"
    else:
        extracted = heuristic_extract(raw, source_channel, item.source_link)

    extracted.source_channel = source_channel
    if item.source_link:
        extracted.source_link = item.source_link

    store.update_state(
        conn, item.id, EXTRACTED,
        from_state=item.state, kind=KIND_DETERMINISTIC, actor=ACTOR_SYSTEM,
        reason=f"extract ({used})", extracted_json=serialize(extracted),
    )
    return AdvanceResult("moved", item.id, item.state, EXTRACTED, "T1", f"extract:{used}")


# Reserved key in extracted_json holding the Sonnet rationale (transparency
# surface shown on the card). Stored alongside the §4 schema fields.
REASONING_KEY = "Обоснование"


def _store_reasoning(extracted_json: str, reasoning: str) -> str:
    """Merge the Sonnet rationale into the extracted_json blob (reserved key)."""
    return agents.merge_aux(extracted_json, REASONING_KEY, reasoning)


def _do_score(conn: psycopg.Connection, item: WorkItem, deps: Deps) -> AdvanceResult:
    """T2: extracted -> scored.

    Routed judgment:
      1) LENIENT deterministic prefilter drops only obvious non-jobs (score 0,
         no LLM call for junk).
      2) Survivors are scored by the Sonnet rubric judge (relevance_score +
         Обоснование). The salary floor is NOT applied here — it is a separate
         deterministic guard at T3/T4.
    """
    extracted = _load_extracted(item)
    if extracted is None:
        return AdvanceResult("noop", item.id, item.state, reason="missing extracted_json")

    raw = item.raw_text or ""
    pf = scoring.prefilter(extracted, raw)
    if not pf.keep:
        score = 0
        reasoning = f"prefilter: {pf.reason}"
        used = "prefilter-drop"
    else:
        reasoning = ""
        used = "sonnet"
        if deps.llm_client is not None:
            try:
                verdict = llm_score(
                    deps.llm_client, extracted, raw, model=deps.judge_model,
                    profile=deps.profile,
                )
                score = scoring.clamp_score(verdict["score"])
                reasoning = verdict.get("reasoning", "")
            except Exception as exc:  # noqa: BLE001 - degrade, never crash T2
                score = 0
                reasoning = f"score failed ({exc}); defaulted low"
                used = "sonnet(error)"
        else:
            # No judge available: keep the item visible to a human rather than
            # silently rejecting. Score at the surface threshold (lenient).
            score = scoring.SURFACE_THRESHOLD
            reasoning = "no LLM judge configured; surfaced for manual review"
            used = "no-llm"

    extracted.relevance_score = float(score)
    extracted.reasons = [reasoning] if reasoning else []
    blob = _store_reasoning(serialize(extracted), reasoning)

    store.update_state(
        conn, item.id, SCORED,
        from_state=item.state, kind=KIND_DETERMINISTIC, actor=ACTOR_SYSTEM,
        reason=f"score={score} ({used})",
        extracted_json=blob, relevance_score=float(score),
    )
    return AdvanceResult(
        "moved", item.id, item.state, SCORED, "T2", f"score={score}",
        extra={"score": score, "reasoning": reasoning, "prefilter_keep": pf.keep},
    )


def _do_reject_or_surface(conn: psycopg.Connection, item: WorkItem, deps: Deps) -> AdvanceResult:
    """T3/T4: scored -> rejected | surfaced.

    Deterministic only:
      - HARD salary guard wins: if the RUB-equivalent salary top is known and
        below the floor, reject regardless of the (Sonnet) relevance score.
      - Otherwise the surface threshold T is applied to the stored score.
    """
    extracted = _load_extracted(item)
    if extracted is None:
        return AdvanceResult("noop", item.id, item.state, reason="missing extracted_json")

    salary_rub = _salary_max_rub(extracted, deps)
    floor_rub = _salary_floor_rub(deps)
    if scoring.salary_guard_reject(salary_rub, floor_rub):
        store.update_state(
            conn, item.id, REJECTED,
            from_state=item.state, kind=KIND_DETERMINISTIC, actor=ACTOR_SYSTEM,
            reason=f"hard reject: salary below EUR {deps.profile.salary_floor_eur:.0f}/mo floor",
        )
        return AdvanceResult(
            "moved", item.id, item.state, REJECTED, "T3", "hard reject (salary)",
            extra={"salary_rub": salary_rub, "floor_rub": floor_rub},
        )

    score = extracted.relevance_score if extracted.relevance_score is not None else 0
    if passes_threshold(score):
        store.update_state(
            conn, item.id, SURFACED,
            from_state=item.state, kind=KIND_DETERMINISTIC, actor=ACTOR_SYSTEM,
            reason=f"score {scoring.clamp_score(score)} >= T",
        )
        return AdvanceResult(
            "moved", item.id, item.state, SURFACED, "T4", "above threshold",
            extra={"score": scoring.clamp_score(score)},
        )

    store.update_state(
        conn, item.id, REJECTED,
        from_state=item.state, kind=KIND_DETERMINISTIC, actor=ACTOR_SYSTEM,
        reason=f"score {scoring.clamp_score(score)} < T",
    )
    return AdvanceResult(
        "moved", item.id, item.state, REJECTED, "T3", "below threshold",
        extra={"score": scoring.clamp_score(score)},
    )


def _do_research(conn: psycopg.Connection, item: WorkItem, deps: Deps) -> AdvanceResult:
    """T10: approved -> researched (agent/LLM)."""
    extracted = _load_extracted(item)
    if extracted is None:
        return AdvanceResult("noop", item.id, item.state, reason="missing extracted_json")
    if deps.llm_client is None:
        return AdvanceResult("needs_human", item.id, item.state, reason="no LLM client for research")

    research_data = agents.research(
        deps.llm_client, extracted, item.raw_text or "", model=deps.cheap_model
    )
    merged = agents.merge_aux(item.extracted_json, "research", research_data)
    store.update_state(
        conn, item.id, RESEARCHED,
        from_state=item.state, kind=KIND_AGENT, actor=ACTOR_AGENT,
        reason="research complete", extracted_json=merged,
    )
    return AdvanceResult("moved", item.id, item.state, RESEARCHED, "T10", "researched",
                         extra={"research": research_data})


def _do_draft(conn: psycopg.Connection, item: WorkItem, deps: Deps) -> AdvanceResult:
    """T11: researched -> drafted (agent/LLM)."""
    extracted = _load_extracted(item)
    if extracted is None:
        return AdvanceResult("noop", item.id, item.state, reason="missing extracted_json")
    if deps.llm_client is None:
        return AdvanceResult("needs_human", item.id, item.state, reason="no LLM client for draft")

    research_data = None
    try:
        existing = json.loads(item.extracted_json or "{}")
        research_data = existing.get("research")
    except Exception:
        pass

    draft_text = agents.draft(
        deps.llm_client, extracted, item.raw_text or "", research_data,
        model=deps.cheap_model, profile=deps.profile,
    )
    merged = agents.merge_aux(item.extracted_json, "draft", draft_text)
    store.update_state(
        conn, item.id, DRAFTED,
        from_state=item.state, kind=KIND_AGENT, actor=ACTOR_AGENT,
        reason="draft generated", extracted_json=merged,
    )
    return AdvanceResult("moved", item.id, item.state, DRAFTED, "T11", "drafted",
                         extra={"draft": draft_text})


def _do_close(conn: psycopg.Connection, item: WorkItem, deps: Deps) -> AdvanceResult:
    """T13: sent -> closed (deterministic)."""
    store.update_state(
        conn, item.id, CLOSED,
        from_state=item.state, kind=KIND_DETERMINISTIC, actor=ACTOR_SYSTEM,
        reason="archived",
    )
    return AdvanceResult("moved", item.id, item.state, CLOSED, "T13", "closed")


# Map current state -> deterministic/agent handler (HITL handled separately).
_AUTO_HANDLERS = {
    DISCOVERED: _do_extract,
    EXTRACTED: _do_score,
    SCORED: _do_reject_or_surface,
    APPROVED: _do_research,
    RESEARCHED: _do_draft,
    SENT: _do_close,
}


# --- The dispatcher ---------------------------------------------------------


def advance(
    conn: psycopg.Connection,
    item: WorkItem,
    decision: Optional[str] = None,
    deps: Optional[Deps] = None,
    reason: Optional[str] = None,
) -> AdvanceResult:
    """Single dispatcher for ALL transitions (DESIGN.md §1.5).

    Args:
        conn: open sqlite connection.
        item: the work item to advance (as last read).
        decision: a human decision (states.DECISION_*) for HITL gates.
        deps: injected LLM/FX collaborators.
        reason: optional human-supplied note recorded with HITL moves.
    """
    deps = deps or Deps()

    # 7) Idempotency: terminal items never move.
    if is_terminal(item.state):
        return AdvanceResult("terminal", item.id, item.state, reason="terminal state")

    outgoing = allowed_transitions(item.state)
    if not outgoing:
        return AdvanceResult("noop", item.id, item.state, reason="no outgoing transitions")

    # 4) HITL: if a decision was supplied, validate + apply it.
    if decision is not None:
        t = transition_for_decision(item.state, decision)
        if t is None:
            return AdvanceResult(
                "noop", item.id, item.state,
                reason=f"decision '{decision}' illegal from {item.state}",
            )
        store.update_state(
            conn, item.id, t.to_state,
            from_state=item.state, kind=KIND_HITL, actor=ACTOR_HUMAN,
            reason=reason or f"human:{decision}",
        )
        return AdvanceResult("moved", item.id, item.state, t.to_state, t.name,
                             reason or decision)

    # No decision supplied: run the automatic handler for this state, if any.
    handler = _AUTO_HANDLERS.get(item.state)
    if handler is not None:
        return handler(conn, item, deps)

    # 4) Otherwise this state only has HITL exits and none was supplied.
    hitl = [t for t in outgoing if t.kind == KIND_HITL]
    if hitl:
        return AdvanceResult(
            "needs_human", item.id, item.state,
            reason="human decision required",
            extra={"options": [t.decision for t in hitl]},
        )

    return AdvanceResult("noop", item.id, item.state, reason="nothing to do")


def advance_by_id(
    conn: psycopg.Connection,
    item_id: int,
    decision: Optional[str] = None,
    deps: Optional[Deps] = None,
    reason: Optional[str] = None,
) -> AdvanceResult:
    """Re-read the item by id (fresh state) then advance it."""
    item = store.get_item(conn, item_id)
    if item is None:
        return AdvanceResult("noop", item_id, "?", reason="item not found")
    return advance(conn, item, decision=decision, deps=deps, reason=reason)


def run_to_gate(
    conn: psycopg.Connection, item_id: int, deps: Optional[Deps] = None, max_steps: int = 10
) -> List[AdvanceResult]:
    """Drive an item through all automatic transitions until it needs a human
    or reaches a terminal state. Used by ingestion to surface candidates."""
    results: List[AdvanceResult] = []
    for _ in range(max_steps):
        res = advance_by_id(conn, item_id, deps=deps)
        results.append(res)
        if res.status in ("needs_human", "terminal", "noop"):
            break
    return results
