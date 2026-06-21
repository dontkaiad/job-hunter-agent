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


def test_expected_edge_count():
    # T1..T13 exactly.
    assert len(S.TRANSITIONS) == 13
    names = {t.name for t in S.TRANSITIONS}
    assert names == {f"T{i}" for i in range(1, 14)}
