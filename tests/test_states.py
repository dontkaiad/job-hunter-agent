"""Transition map legality and terminal detection (pure)."""

from job_hunter import states as S


def test_all_transitions_reference_valid_states():
    for t in S.TRANSITIONS:
        assert S.is_valid_state(t.from_state), t
        assert S.is_valid_state(t.to_state), t
        assert t.kind in (S.KIND_DETERMINISTIC, S.KIND_HITL, S.KIND_AGENT)


def test_terminal_states_have_no_outgoing():
    for term in S.TERMINAL_STATES:
        assert S.is_terminal(term)
        assert S.allowed_transitions(term) == []


def test_non_terminal_states_have_outgoing():
    non_terminal = set(S.ALL_STATES) - set(S.TERMINAL_STATES)
    for st in non_terminal:
        assert S.allowed_transitions(st), f"{st} should have outgoing transitions"


def test_hitl_transitions_carry_decisions():
    for t in S.TRANSITIONS:
        if t.kind == S.KIND_HITL:
            assert t.decision is not None
        else:
            assert t.decision is None


def test_transition_for_decision_surfaced():
    t = S.transition_for_decision(S.SURFACED, S.DECISION_APPROVE)
    assert t is not None and t.to_state == S.APPROVED and t.name == "T7"

    assert S.transition_for_decision(S.SURFACED, S.DECISION_SEND) is None
    assert S.transition_for_decision(S.SCORED, S.DECISION_APPROVE) is None


def test_backlog_can_promote_or_drop():
    assert S.transition_for_decision(S.BACKLOG, S.DECISION_APPROVE).to_state == S.APPROVED
    assert S.transition_for_decision(S.BACKLOG, S.DECISION_SKIP).to_state == S.SKIPPED


def test_sent_has_no_deterministic_auto_close():
    """The old deterministic T13 sent->closed is gone: SENT only has manual
    (HITL) exits now, so a decision-less advance() must NOT auto-close."""
    outgoing = S.allowed_transitions(S.SENT)
    assert outgoing, "SENT must still have outgoing (the funnel)"
    assert all(t.kind == S.KIND_HITL for t in outgoing)
    assert {t.to_state for t in outgoing} == {S.SCREENING, S.DECLINED, S.CLOSED}


def test_post_send_funnel_transitions():
    cases = [
        (S.SENT, S.DECISION_SCREENING, S.SCREENING),
        (S.SENT, S.DECISION_DECLINE, S.DECLINED),
        (S.SENT, S.DECISION_CLOSE, S.CLOSED),
        (S.SCREENING, S.DECISION_INTERVIEW, S.INTERVIEW),
        (S.SCREENING, S.DECISION_DECLINE, S.DECLINED),
        (S.SCREENING, S.DECISION_CLOSE, S.CLOSED),
        (S.INTERVIEW, S.DECISION_OFFER, S.OFFER),
        (S.INTERVIEW, S.DECISION_DECLINE, S.DECLINED),
        (S.INTERVIEW, S.DECISION_CLOSE, S.CLOSED),
    ]
    for state, decision, expected in cases:
        t = S.transition_for_decision(state, decision)
        assert t is not None, (state, decision)
        assert t.to_state == expected
        assert t.kind == S.KIND_HITL


def test_offer_and_declined_are_terminal():
    for term in (S.OFFER, S.DECLINED):
        assert S.is_terminal(term)
        assert S.allowed_transitions(term) == []


def test_declined_is_distinct_from_scoring_rejected():
    """Employer rejection (declined) must NOT be the scoring reject (rejected),
    and there is no path between them."""
    assert S.DECLINED != S.REJECTED
    assert S.transition_for_decision(S.SENT, S.DECISION_DECLINE).to_state == S.DECLINED
    # REJECTED stays the scored-stage outcome (T3); nothing routes to it via a
    # post-send decision.
    assert all(t.to_state != S.REJECTED for t in S.allowed_transitions(S.SENT))


def test_offer_not_reachable_before_interview():
    assert S.transition_for_decision(S.SENT, S.DECISION_OFFER) is None
    assert S.transition_for_decision(S.SCREENING, S.DECISION_OFFER) is None


def test_expected_edge_count():
    # T1..T21 exactly (T13..T21 are the post-send response funnel).
    assert len(S.TRANSITIONS) == 21
    names = {t.name for t in S.TRANSITIONS}
    assert names == {f"T{i}" for i in range(1, 22)}
