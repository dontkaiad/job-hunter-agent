"""PURE: state constants, the transition map, transition kinds, helpers.

No I/O, no clock, no DB. See DESIGN.md §1.
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional

# --- States -----------------------------------------------------------------

DISCOVERED = "discovered"
EXTRACTED = "extracted"
SCORED = "scored"
REJECTED = "rejected"
SURFACED = "surfaced"
SKIPPED = "skipped"
BACKLOG = "backlog"
APPROVED = "approved"
RESEARCHED = "researched"
DRAFTED = "drafted"
SENT = "sent"
CLOSED = "closed"
# Post-send response funnel (visibility into the employer's reply).
SCREENING = "screening"   # employer replied; first contact / screening
INTERVIEW = "interview"   # interview stage
OFFER = "offer"           # offer received (terminal)
DECLINED = "declined"     # employer rejection (terminal) — NOT the scoring
                          # REJECTED (T3); that one is the relevance filter.

ALL_STATES = (
    DISCOVERED, EXTRACTED, SCORED, REJECTED, SURFACED, SKIPPED,
    BACKLOG, APPROVED, RESEARCHED, DRAFTED, SENT, CLOSED,
    SCREENING, INTERVIEW, OFFER, DECLINED,
)

TERMINAL_STATES = frozenset({REJECTED, SKIPPED, CLOSED, OFFER, DECLINED})

# --- Transition kinds -------------------------------------------------------

KIND_DETERMINISTIC = "deterministic"
KIND_HITL = "hitl"
KIND_AGENT = "agent"

# --- HITL decisions ---------------------------------------------------------
# Decisions a human can supply to advance() at a gate.

DECISION_SKIP = "skip"
DECISION_BACKLOG = "backlog"
DECISION_APPROVE = "approve"
DECISION_SEND = "send"
# Post-send response funnel decisions (all manual; the operator taps a button).
DECISION_SCREENING = "screening"
DECISION_INTERVIEW = "interview"
DECISION_OFFER = "offer"
DECISION_DECLINE = "decline"
DECISION_CLOSE = "close"


class Transition(NamedTuple):
    """A single edge in the state machine."""

    name: str           # T1..T21
    from_state: str
    to_state: str
    kind: str           # deterministic | hitl | agent
    decision: Optional[str] = None  # required human decision for HITL edges


# Static transition table (DESIGN.md §1). Source of truth for advance().
TRANSITIONS: List[Transition] = [
    Transition("T1", DISCOVERED, EXTRACTED, KIND_DETERMINISTIC),
    Transition("T2", EXTRACTED, SCORED, KIND_DETERMINISTIC),
    Transition("T3", SCORED, REJECTED, KIND_DETERMINISTIC),
    Transition("T4", SCORED, SURFACED, KIND_DETERMINISTIC),
    Transition("T5", SURFACED, SKIPPED, KIND_HITL, DECISION_SKIP),
    Transition("T6", SURFACED, BACKLOG, KIND_HITL, DECISION_BACKLOG),
    Transition("T7", SURFACED, APPROVED, KIND_HITL, DECISION_APPROVE),
    Transition("T8", BACKLOG, APPROVED, KIND_HITL, DECISION_APPROVE),
    Transition("T9", BACKLOG, SKIPPED, KIND_HITL, DECISION_SKIP),
    Transition("T10", APPROVED, RESEARCHED, KIND_AGENT),
    Transition("T11", RESEARCHED, DRAFTED, KIND_AGENT),
    Transition("T12", DRAFTED, SENT, KIND_HITL, DECISION_SEND),
    # Post-send response funnel. ALL manual (KIND_HITL): the operator records
    # the employer's reply by tapping a button. There is NO auto sent->closed
    # any more (the old deterministic T13); SENT now waits for a human decision.
    Transition("T13", SENT, SCREENING, KIND_HITL, DECISION_SCREENING),
    Transition("T14", SENT, DECLINED, KIND_HITL, DECISION_DECLINE),
    Transition("T15", SENT, CLOSED, KIND_HITL, DECISION_CLOSE),
    Transition("T16", SCREENING, INTERVIEW, KIND_HITL, DECISION_INTERVIEW),
    Transition("T17", SCREENING, DECLINED, KIND_HITL, DECISION_DECLINE),
    Transition("T18", SCREENING, CLOSED, KIND_HITL, DECISION_CLOSE),
    Transition("T19", INTERVIEW, OFFER, KIND_HITL, DECISION_OFFER),
    Transition("T20", INTERVIEW, DECLINED, KIND_HITL, DECISION_DECLINE),
    Transition("T21", INTERVIEW, CLOSED, KIND_HITL, DECISION_CLOSE),
]

# Index: from_state -> list of outgoing transitions.
_BY_FROM: Dict[str, List[Transition]] = {}
for _t in TRANSITIONS:
    _BY_FROM.setdefault(_t.from_state, []).append(_t)


def is_terminal(state: str) -> bool:
    """True when no transition can leave this state."""
    return state in TERMINAL_STATES


def allowed_transitions(state: str) -> List[Transition]:
    """Return the outgoing transitions legal from ``state`` (possibly empty)."""
    return list(_BY_FROM.get(state, ()))


def transition_for_decision(state: str, decision: str) -> Optional[Transition]:
    """Find the HITL transition out of ``state`` matching ``decision``.

    Returns None when the decision is not legal from this state.
    """
    for t in allowed_transitions(state):
        if t.kind == KIND_HITL and t.decision == decision:
            return t
    return None


def is_valid_state(state: str) -> bool:
    return state in ALL_STATES
