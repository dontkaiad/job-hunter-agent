"""Adversarial tests for the WRITE/action endpoints (issue #4) — gaps not covered
by the Developer's test_webapi_actions.py.

Added by the Tester role. Tests are additive only — no production code changed.

Gaps addressed:
  1. Twin-item state_transitions equivalence: kind/actor/from_state/to_state must match.
  2. Draft from non-approved/researched states -> 409 (backlog, sent, skipped).
  3. sent on approved (not drafted) -> 409, item state unchanged.
  4. skip on sent -> 409, item state unchanged.
  5. Persistence via SEPARATE connection for /sent (spec requires approve AND sent).
  6. Auth gate: overriding require_writer to HTTPException(401) blocks all 5 writes
     but GET routes remain unaffected.
  7. /draft with NO LLM wired -> 409 (not 500), item state unchanged (approve -> stays approved).
  8. History present in response for non-sent actions (approve, skip, backlog).
  9. No-partial-write on 409: state is UNCHANGED after a failed transition.
 10. backlog-on-backlog -> 409 (no T6 from backlog).
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from job_hunter import pipeline, store, webapi
from job_hunter.pipeline import Deps
from job_hunter.states import (
    APPROVED,
    BACKLOG,
    DECISION_APPROVE,
    DECISION_BACKLOG,
    DECISION_SEND,
    DECISION_SKIP,
    DRAFTED,
    SENT,
    SKIPPED,
    SURFACED,
)
from tests.conftest import (
    TEST_SUPERUSER_ID,
    FakeFx,
    FakeLLM,
    apply_auth_overrides,
    make_session_cookie,
)


# ---------------------------------------------------------------------------
# Helpers (mirrored from test_webapi_actions.py so this file is self-contained)
# ---------------------------------------------------------------------------


def _extracted(**over) -> str:
    base = {
        "title": "Backend Engineer",
        "source_channel": "ch",
        "company": "Acme",
        "stack": ["python", "fastapi"],
        "salary_min": 200000,
        "salary_max": 300000,
        "currency": "RUB",
        "remote": True,
        "reasons": ["good fit"],
        "benefits": ["dms"],
    }
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


_SEED_CTR = [1000]  # unique counter so this file doesn't collide with other files


def _seed(conn, *, state, score=85.0, extracted=None):
    """Insert + fast-forward an item to ``state`` using real store helpers."""
    extracted = _extracted() if extracted is None else extracted
    _SEED_CTR[0] += 1
    item_id = store.insert_item(
        conn,
        raw_text="raw job text",
        source_channel="ch",
        source_link="http://t.me/x/1",
        source_message_id=str(_SEED_CTR[0]),
    )
    store.set_extracted(conn, item_id, extracted)
    if state != "discovered":
        store.update_state(
            conn,
            item_id,
            state,
            from_state="discovered",
            kind="deterministic",
            actor="system",
            reason="test seed",
            extracted_json=extracted,
            relevance_score=score,
        )
    return item_id


def _draft_deps() -> Deps:
    """Deps with a scripted FakeLLM so run_to_gate reaches DRAFTED."""
    fake = FakeLLM()
    fake.set_for("research", '{"summary":"s","talking_points":[],"questions":[]}')
    fake.set_for("application message", "Hello, I'd love to apply.")
    return Deps(llm_client=fake, fx=FakeFx(), use_llm_extract=False)


@pytest.fixture
def client(conn, auth_conn):
    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    app.dependency_overrides[webapi.get_fx] = lambda: FakeFx()
    app.dependency_overrides[webapi.get_deps] = _draft_deps
    apply_auth_overrides(app, auth_conn)
    with TestClient(app) as c:
        c.cookies.set("hl_session", make_session_cookie(TEST_SUPERUSER_ID))
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. Twin-item state_transitions equivalence (kind / actor / from / to)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,decision,from_state,expected_to",
    [
        ("approve", DECISION_APPROVE, SURFACED, APPROVED),
        ("approve", DECISION_APPROVE, BACKLOG, APPROVED),
        ("skip", DECISION_SKIP, SURFACED, SKIPPED),
        ("skip", DECISION_SKIP, BACKLOG, SKIPPED),
        ("backlog", DECISION_BACKLOG, SURFACED, BACKLOG),
        ("sent", DECISION_SEND, DRAFTED, SENT),
    ],
)
def test_twin_transition_row_matches_bot(
    client, conn, action, decision, from_state, expected_to
):
    """The endpoint's state_transitions row must have identical kind/actor/from/to
    as an equivalent advance_by_id call — proving the endpoint IS the bot path."""
    # Drive through the endpoint.
    item_id = _seed(conn, state=from_state)
    resp = client.post(f"/api/items/{item_id}/{action}")
    assert resp.status_code == 200, resp.text

    # Drive a twin directly.
    twin_id = _seed(conn, state=from_state)
    pipeline.advance_by_id(conn, twin_id, decision=decision, deps=_draft_deps())

    # Read the last transition row for each.
    def last_row(iid):
        rows = store.list_transitions(conn, iid)
        # The last row is the one we care about (seed also writes a row).
        return rows[-1]

    ep_row = last_row(item_id)
    tw_row = last_row(twin_id)

    assert ep_row["to_state"] == tw_row["to_state"], "to_state mismatch"
    assert ep_row["from_state"] == tw_row["from_state"], "from_state mismatch"
    assert ep_row["kind"] == tw_row["kind"], "kind mismatch"
    assert ep_row["actor"] == tw_row["actor"], "actor mismatch"


# ---------------------------------------------------------------------------
# 2. draft from non-approved/researched states -> 409 (spec §4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_state",
    [BACKLOG, SENT, SKIPPED],
)
def test_draft_from_invalid_state_409(client, conn, bad_state):
    """draft endpoint must return 409 for any state that is not approved/researched/drafted."""
    item_id = _seed(conn, state=bad_state)
    resp = client.post(f"/api/items/{item_id}/draft")
    assert resp.status_code == 409, f"expected 409 from state {bad_state!r}, got {resp.status_code}"
    # State must be unchanged.
    assert store.get_item(conn, item_id).state == bad_state


# ---------------------------------------------------------------------------
# 3. sent on approved (not drafted) -> 409, state unchanged
# ---------------------------------------------------------------------------


def test_sent_on_approved_409_state_unchanged(client, conn):
    """POST /sent on an approved (not yet drafted) item must 409; item stays approved."""
    item_id = _seed(conn, state=APPROVED)
    resp = client.post(f"/api/items/{item_id}/sent")
    assert resp.status_code == 409
    assert store.get_item(conn, item_id).state == APPROVED  # no partial write


# ---------------------------------------------------------------------------
# 4. skip on sent -> 409, state unchanged
# ---------------------------------------------------------------------------


def test_skip_on_sent_409_state_unchanged(client, conn):
    """POST /skip on a sent item must 409; item stays sent."""
    item_id = _seed(conn, state=SENT)
    resp = client.post(f"/api/items/{item_id}/skip")
    assert resp.status_code == 409
    assert store.get_item(conn, item_id).state == SENT  # no partial write


# ---------------------------------------------------------------------------
# 5. Persistence via SEPARATE connection for /sent
# ---------------------------------------------------------------------------


def test_sent_action_persists_via_separate_connection(client, conn, pg_dsn):
    """After POST /sent, re-reading through an INDEPENDENT connection must return SENT.

    Spec requires persistence to be proven for BOTH approve and sent.
    test_webapi_actions.py covers approve only; this test covers sent.
    """
    item_id = _seed(conn, state=DRAFTED)
    # Pre-seed a draft blob so the response shape is complete.
    blob = json.loads(_extracted())
    blob["draft"] = "pre-existing draft"
    store.set_extracted(conn, item_id, json.dumps(blob, ensure_ascii=False))

    resp = client.post(f"/api/items/{item_id}/sent")
    assert resp.status_code == 200, resp.text

    # Fresh independent connection — proves advance() committed.
    other = store.connect(pg_dsn)
    try:
        reread = store.get_item(other, item_id)
    finally:
        other.close()
    assert reread is not None
    assert reread.state == SENT


# ---------------------------------------------------------------------------
# 6. Auth gate: overriding require_writer to raise 401 blocks writes, not reads
# ---------------------------------------------------------------------------


def test_auth_gate_blocks_all_api_without_session(conn, auth_conn):
    """Issue #5: with NO session cookie, EVERY /api route (the 2 GET reads and
    all 5 POST writes) must 401. The #4 require_writer no-op folded into
    require_auth, so reads are now gated too.
    """
    item_id = _seed(conn, state=SURFACED)

    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    app.dependency_overrides[webapi.get_fx] = lambda: FakeFx()
    app.dependency_overrides[webapi.get_deps] = _draft_deps
    apply_auth_overrides(app, auth_conn)

    with TestClient(app) as c:  # NOTE: no session cookie set
        for action in ("approve", "skip", "backlog", "sent", "draft"):
            r = c.post(f"/api/items/{item_id}/{action}")
            assert r.status_code == 401, (
                f"write route /{action} should be 401, got {r.status_code}"
            )
        # GET routes are NOW also gated (issue #5).
        assert c.get("/api/pipeline").status_code == 401
        assert c.get(f"/api/items/{item_id}").status_code == 401

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 7. /draft with NO LLM wired -> 409, item state unchanged (not 500)
# ---------------------------------------------------------------------------


def test_draft_without_llm_returns_409_not_500(conn, auth_conn):
    """/draft when deps has llm_client=None must return 409 (not 500) and the
    item must stay in 'approved' — no partial write to 'researched'.

    Verifies: run_to_gate stops at 'needs_human' when research step has no LLM,
    so item remains APPROVED. webapi then correctly returns 409.
    """
    no_llm_deps = Deps(llm_client=None, fx=FakeFx(), use_llm_extract=False)

    item_id = _seed(conn, state=APPROVED)

    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    app.dependency_overrides[webapi.get_fx] = lambda: FakeFx()
    app.dependency_overrides[webapi.get_deps] = lambda: no_llm_deps
    apply_auth_overrides(app, auth_conn)

    with TestClient(app) as c:
        c.cookies.set("hl_session", make_session_cookie(TEST_SUPERUSER_ID))
        resp = c.post(f"/api/items/{item_id}/draft")

    app.dependency_overrides.clear()

    assert resp.status_code == 409, f"expected 409, got {resp.status_code}: {resp.text}"
    # Item must not have advanced (no partial write).
    after = store.get_item(conn, item_id)
    assert after.state == APPROVED, (
        f"item should stay APPROVED without LLM, but is now {after.state!r}"
    )


# ---------------------------------------------------------------------------
# 8. History in response for non-sent actions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,from_state,expected_to",
    [
        ("approve", SURFACED, APPROVED),
        ("skip", SURFACED, SKIPPED),
        ("backlog", SURFACED, BACKLOG),
    ],
)
def test_response_history_includes_new_transition(client, conn, action, from_state, expected_to):
    """The ItemDetail response for each action must include the just-applied transition
    in the history list (to_state matches expected, history non-empty).
    """
    item_id = _seed(conn, state=from_state)
    resp = client.post(f"/api/items/{item_id}/{action}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    history = body["history"]
    assert isinstance(history, list) and len(history) >= 1
    assert body["status"] == expected_to
    # Last transition must reflect the new move.
    assert history[-1]["to_state"] == expected_to


# ---------------------------------------------------------------------------
# 9. No-partial-write on 409: double-approve leaves item UNCHANGED
# ---------------------------------------------------------------------------


def test_double_approve_state_unchanged_after_409(client, conn):
    """After a 409 on the second approve, the item's state must remain 'approved'
    (not back-slid to anything else). Verifies the 'noop' path in advance() does
    NOT write any row.
    """
    item_id = _seed(conn, state=SURFACED)

    first = client.post(f"/api/items/{item_id}/approve")
    assert first.status_code == 200

    transitions_after_first = len(store.list_transitions(conn, item_id))

    second = client.post(f"/api/items/{item_id}/approve")
    assert second.status_code == 409

    # State must still be APPROVED.
    assert store.get_item(conn, item_id).state == APPROVED

    # No new transition row must have been written.
    transitions_after_second = len(store.list_transitions(conn, item_id))
    assert transitions_after_second == transitions_after_first, (
        "a 409 must not append a new state_transitions row"
    )


# ---------------------------------------------------------------------------
# 10. backlog on backlog -> 409 (T6 is only from surfaced)
# ---------------------------------------------------------------------------


def test_backlog_on_backlog_409_state_unchanged(client, conn):
    """POST /backlog on an already-backlogged item must return 409; item stays backlog."""
    item_id = _seed(conn, state=BACKLOG)
    resp = client.post(f"/api/items/{item_id}/backlog")
    assert resp.status_code == 409
    assert store.get_item(conn, item_id).state == BACKLOG
