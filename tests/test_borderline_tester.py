"""Tester-added tests for the borderline band (scores 50–59) feature.

Covers gaps identified during validation against the spec:
  1. Float boundary precision: 49.9 excluded, 50.0 included, 59.9 included,
     60.0 EXCLUDED (must be excluded — SURFACE_THRESHOLD=60 stays in surfaced).
  2. State-agnostic band: band items in multiple states (rejected, skipped,
     scored, surfaced) all appear; a >=60 surfaced item does NOT appear.
  3. Read-only proof: snapshot state_transitions total count BEFORE, run
     handle_borderline AND the API band query, assert count unchanged AND no
     state changed.
  4. No reply_markup in handle_borderline reply (no inline buttons).
  5. render_borderline graceful with missing company/title/link fields.
  6. Long-output risk: >4096-char detection — asserts render_borderline has NO
     built-in cap (documents the risk; tested structurally, no real send).
  7. API band read-only: state_transitions count before/after.
  8. Total state_transitions count invariant (atomic with state_transitions).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from job_hunter import bot, pipeline, store, webapi
from job_hunter.config import Config
from job_hunter.states import REJECTED, SKIPPED, SCORED, SURFACED, APPROVED, SENT
from tests.conftest import (
    TEST_SUPERUSER_ID,
    FakeFx,
    apply_auth_overrides,
    make_session_cookie,
)

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

ALLOWED_UID = 777


def _cfg(allowed=None):
    return Config(
        bot_token="x",
        notify_chat_id=123,
        allowed_user_ids={ALLOWED_UID} if allowed is None else set(allowed),
    )


class FakeAnswerMessage:
    """Captures message.answer() calls without network."""

    def __init__(self, user_id=ALLOWED_UID):
        self.from_user = type("U", (), {"id": user_id})()
        self.replies = []

    async def answer(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})


def _seed(conn, *, score, msg_id, state=REJECTED, company="Acme",
          title="Backend", link="https://t.me/jobs/1"):
    """Insert item with extracted_json + score, move to `state`."""
    item_id = store.insert_item(
        conn, raw_text="x", source_channel="@c",
        source_message_id=str(msg_id), source_link=link,
    )
    ex = json.dumps({"company": company, "title": title}, ensure_ascii=False)
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=ex, relevance_score=score)
    if state != "extracted":
        store.update_state(conn, item_id, state, from_state="extracted",
                           kind="deterministic", actor="system")
    return item_id


def _total_transitions(conn) -> int:
    """Return total row count in state_transitions (all items)."""
    row = conn.execute("SELECT COUNT(*) AS n FROM state_transitions").fetchone()
    return row["n"]


@pytest.fixture
def client(conn, auth_conn):
    """Authenticated TestClient for the webapi app, injecting the test conn."""
    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    app.dependency_overrides[webapi.get_fx] = lambda: FakeFx()
    apply_auth_overrides(app, auth_conn)
    with TestClient(app) as c:
        c.cookies.set("hl_session", make_session_cookie(TEST_SUPERUSER_ID))
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. Float boundary precision: 49.9, 50.0, 59.9, 60.0 exact
# ---------------------------------------------------------------------------

def test_store_band_float_boundary_precise(conn):
    """Half-open band [50, 60) must handle non-integer floats correctly:
    - 49.9 -> EXCLUDED (below 50)
    - 50.0 -> INCLUDED (= min, inclusive lower bound)
    - 59.9 -> INCLUDED (< 60)
    - 60.0 -> EXCLUDED (= max, exclusive upper bound; SURFACE_THRESHOLD)
    """
    store.insert_item(conn, raw_text="x", source_channel="@c", source_message_id="1")
    ids = {}
    for score, mid in [(49.9, 1), (50.0, 2), (59.9, 3), (60.0, 4)]:
        ids[score] = store.insert_item(conn, raw_text="x", source_channel="@c",
                                       source_message_id=str(mid + 10))
        conn.execute("UPDATE work_items SET relevance_score = %s WHERE id = %s",
                     (score, ids[score]))
    conn.commit()

    items = store.list_pipeline(conn, min_score=50.0, max_score=60.0)
    scores = {it.relevance_score for it in items}

    assert 49.9 not in scores, "49.9 must be EXCLUDED (below lower bound 50)"
    assert 50.0 in scores, "50.0 must be INCLUDED (= lower bound, inclusive)"
    assert 59.9 in scores, "59.9 must be INCLUDED (< upper bound 60)"
    assert 60.0 not in scores, (
        "60.0 must be EXCLUDED (= upper bound, exclusive; "
        "SURFACE_THRESHOLD=60 stays in surfaced flow, not borderline)"
    )


def test_api_band_float_boundary_precise(client, conn):
    """API endpoint also applies the half-open float boundary correctly."""
    for score, mid in [(49.9, 1), (50.0, 2), (59.9, 3), (60.0, 4)]:
        item_id = store.insert_item(conn, raw_text="x", source_channel="@c",
                                    source_message_id=str(mid + 20))
        conn.execute("UPDATE work_items SET relevance_score = %s WHERE id = %s",
                     (score, item_id))
    conn.commit()

    data = client.get("/api/pipeline", params={"min_score": 50, "max_score": 60}).json()
    result_scores = {x["score"] for x in data}

    assert 49.9 not in result_scores, "49.9 must not appear in API band query"
    assert 50.0 in result_scores, "50.0 must appear in API band query"
    assert 59.9 in result_scores, "59.9 must appear in API band query"
    assert 60.0 not in result_scores, (
        "60.0 must NOT appear in API band query (SURFACE_THRESHOLD boundary)"
    )


def test_bot_borderline_float_boundary(conn, deps):
    """handle_borderline with fractional scores 49.9, 50.0, 59.9, 60.0.

    Band query correctness: only 50.0 and 59.9 should appear (49.9 below lower
    bound, 60.0 equal to exclusive upper bound). KNOWN BUG: render_borderline
    uses f"{score:.0f}" which rounds 59.9 to "60", so the line displays as
    "60 — ..." even though the item IS correctly in the band. The SQL filter is
    correct; the display is misleading (looks like a threshold item). This test
    exposes the bug: exactly 2 lines must be returned, and the source_links
    reveal which items are present regardless of the rounded score string.
    """
    for score, mid in [(49.9, 1), (50.0, 2), (59.9, 3), (60.0, 4)]:
        item_id = store.insert_item(conn, raw_text="x", source_channel="@c",
                                    source_message_id=str(mid + 30),
                                    source_link=f"https://t.me/c/{mid}")
        conn.execute("UPDATE work_items SET relevance_score = %s WHERE id = %s",
                     (score, item_id))
    conn.commit()

    b = bot.JobHunterBot(_cfg(), conn, deps)
    msg = FakeAnswerMessage()
    asyncio.run(b.handle_borderline(msg))

    text = msg.replies[0]["text"]
    lines = text.splitlines()

    # Exactly 2 items should be in the band (score 50.0 and 59.9)
    assert len(lines) == 2, (
        f"Expected 2 lines (scores 50.0 and 59.9); got {len(lines)}: {lines}"
    )

    # Verify by source link which items are present
    assert "https://t.me/c/2" in text, "50.0 item (link /c/2) must appear"
    assert "https://t.me/c/3" in text, "59.9 item (link /c/3) must appear"

    # 49.9 (link /c/1) and 60.0 (link /c/4) must NOT appear
    assert "https://t.me/c/1" not in text, "49.9 item must NOT appear in borderline band"
    assert "https://t.me/c/4" not in text, (
        "60.0 item must NOT appear in band (SURFACE_THRESHOLD boundary)"
    )

    # FIXED: render_borderline TRUNCATES via int(score), so a 59.9 item reads
    # "59" (never "60"). A "60" would look like it cleared SURFACE_THRESHOLD.
    score_strs = [ln.split(" ")[0] for ln in lines]
    assert "59" in score_strs, "59.9 must render as '59' (truncated, not rounded to 60)"
    assert "50" in score_strs, "50.0 must render as '50'"
    assert "60" not in score_strs, "no borderline line may display '60'"


# ---------------------------------------------------------------------------
# 2. State-agnostic: band items in multiple states all returned
# ---------------------------------------------------------------------------

def test_store_band_state_agnostic_all_states_returned(conn):
    """list_pipeline with band filter is state-agnostic: items in rejected,
    skipped, scored, surfaced all appear when in [50, 60). A >=60 surfaced
    item must NOT appear."""
    states_and_msgs = [
        (REJECTED, 1),
        (SKIPPED, 2),
        (SCORED, 3),
        (SURFACED, 4),
    ]
    band_ids = set()
    for state, mid in states_and_msgs:
        iid = _seed(conn, score=55.0, msg_id=mid * 100, state=state,
                    company=f"Co_{state}", title="Job55")
        band_ids.add(iid)

    # A surfaced item at score 70 (>=60) must NOT appear
    out_of_band_id = _seed(conn, score=70.0, msg_id=999, state=SURFACED,
                           company="HighCo", title="Job70")

    items = store.list_pipeline(conn, min_score=50.0, max_score=60.0)
    returned_ids = {it.id for it in items}

    assert band_ids == returned_ids, (
        f"Expected exactly the 4 in-band items; "
        f"got {returned_ids}, expected {band_ids}"
    )
    assert out_of_band_id not in returned_ids, (
        "A >=60 surfaced item must NOT appear in the [50,60) band"
    )


def test_bot_borderline_multi_state_all_returned(conn, deps):
    """handle_borderline returns items in rejected, skipped, scored, surfaced
    states when their score is in [50, 60)."""
    for state, mid in [(REJECTED, 1), (SKIPPED, 2), (SCORED, 3), (SURFACED, 4)]:
        _seed(conn, score=55.0, msg_id=mid * 200, state=state,
              company=f"Co{state}", title=f"T{state}")

    # Out-of-band surfaced (score=70)
    _seed(conn, score=70.0, msg_id=9990, state=SURFACED,
          company="HighCo", title="HighJob")

    b = bot.JobHunterBot(_cfg(), conn, deps)
    msg = FakeAnswerMessage()
    asyncio.run(b.handle_borderline(msg))

    text = msg.replies[0]["text"]
    # All 4 in-band states should contribute lines; they all have score 55
    lines = text.splitlines()
    assert len(lines) == 4, (
        f"Expected 4 lines (one per in-band item across 4 states); got {len(lines)}: {lines}"
    )
    # The out-of-band surfaced item (score 70) must not appear
    assert "HighCo" not in text, ">=60 surfaced item must not appear in borderline output"


# ---------------------------------------------------------------------------
# 3. Read-only proof: state_transitions count invariant
# ---------------------------------------------------------------------------

def test_handle_borderline_state_transitions_count_unchanged(conn, deps):
    """Running handle_borderline must not add ANY row to state_transitions
    (across ALL items in the DB, not just band items)."""
    ids = [
        _seed(conn, score=55.0, msg_id=301, state=REJECTED),
        _seed(conn, score=58.0, msg_id=302, state=REJECTED),
        _seed(conn, score=80.0, msg_id=303, state=SURFACED),
    ]
    n_before = _total_transitions(conn)
    states_before = {i: store.get_item(conn, i).state for i in ids}
    updated_ats_before = {i: store.get_item(conn, i).updated_at for i in ids}

    b = bot.JobHunterBot(_cfg(), conn, deps)
    asyncio.run(b.handle_borderline(FakeAnswerMessage()))

    n_after = _total_transitions(conn)
    states_after = {i: store.get_item(conn, i).state for i in ids}
    updated_ats_after = {i: store.get_item(conn, i).updated_at for i in ids}

    assert n_after == n_before, (
        f"state_transitions count changed from {n_before} to {n_after}; "
        "handle_borderline must be READ-ONLY"
    )
    assert states_after == states_before, "Some item state changed after handle_borderline"
    assert updated_ats_after == updated_ats_before, (
        "Some item's updated_at changed after handle_borderline"
    )


def test_api_band_state_transitions_count_unchanged(client, conn):
    """GET /api/pipeline?min_score=50&max_score=60 must not add to state_transitions."""
    iid = _seed(conn, score=55.0, msg_id=401, state=REJECTED)
    n_before = _total_transitions(conn)
    updated_before = store.get_item(conn, iid).updated_at

    resp = client.get("/api/pipeline", params={"min_score": 50, "max_score": 60})
    assert resp.status_code == 200

    n_after = _total_transitions(conn)
    updated_after = store.get_item(conn, iid).updated_at
    assert n_after == n_before, (
        f"state_transitions count changed: {n_before} -> {n_after}"
    )
    assert updated_after == updated_before, "item updated_at changed after read-only API call"


# ---------------------------------------------------------------------------
# 4. No reply_markup in handle_borderline reply (no inline buttons)
# ---------------------------------------------------------------------------

def test_handle_borderline_reply_has_no_keyboard(conn, deps):
    """The /borderline reply must contain NO reply_markup (no inline buttons).
    It is a READ-ONLY browse view; no actions are presented."""
    _seed(conn, score=55.0, msg_id=501)

    b = bot.JobHunterBot(_cfg(), conn, deps)
    msg = FakeAnswerMessage()
    asyncio.run(b.handle_borderline(msg))

    assert len(msg.replies) == 1
    reply = msg.replies[0]
    # reply_markup must be absent or None (no keyboard)
    assert reply.get("reply_markup") is None, (
        f"handle_borderline must send no reply_markup (no inline buttons); "
        f"got: {reply.get('reply_markup')!r}"
    )


def test_handle_borderline_empty_reply_has_no_keyboard(conn, deps):
    """Even the empty-band reply (нет пограничных вакансий) must have no keyboard."""
    b = bot.JobHunterBot(_cfg(), conn, deps)
    msg = FakeAnswerMessage()
    asyncio.run(b.handle_borderline(msg))

    reply = msg.replies[0]
    assert reply["text"] == bot.BORDERLINE_EMPTY
    assert reply.get("reply_markup") is None, (
        "Empty-band BORDERLINE_EMPTY reply must have no keyboard"
    )


# ---------------------------------------------------------------------------
# 5. render_borderline graceful with missing fields
# ---------------------------------------------------------------------------

def test_render_borderline_missing_company_and_title():
    """render_borderline must not crash when company and title are both missing."""
    class _Item:
        relevance_score = 55.0
        extracted_json = json.dumps({})  # no company, no title
        source_link = "https://t.me/c/1"

    result = bot.render_borderline([_Item()])
    assert "55" in result
    assert "https://t.me/c/1" in result
    assert "None" not in result, "Missing fields must not render as literal 'None'"


def test_render_borderline_null_score():
    """render_borderline must handle NULL relevance_score gracefully (shows '?')."""
    class _Item:
        relevance_score = None
        extracted_json = json.dumps({"company": "X", "title": "Y"})
        source_link = "https://t.me/c/2"

    result = bot.render_borderline([_Item()])
    assert "?" in result, "NULL score must render as '?'"
    assert "X" in result
    assert "Y" in result


def test_render_borderline_garbage_extracted_json():
    """Malformed extracted_json must not crash (graceful fallback to empty)."""
    class _Item:
        relevance_score = 52.0
        extracted_json = "{not valid json !!!"
        source_link = None

    result = bot.render_borderline([_Item()])
    assert "52" in result
    assert "None" not in result


def test_render_borderline_no_source_link():
    """Items without a source_link must not add a trailing space or 'None'."""
    class _Item:
        relevance_score = 53.0
        extracted_json = json.dumps({"company": "Acme", "title": "Dev"})
        source_link = None

    result = bot.render_borderline([_Item()])
    lines = result.splitlines()
    assert len(lines) == 1
    # No trailing space after title when link is absent
    assert not lines[0].endswith(" "), (
        f"Line must not end with a space when source_link is None: {lines[0]!r}"
    )
    assert "None" not in lines[0], "None must not appear in output"


# ---------------------------------------------------------------------------
# 6. Long-output risk: render_borderline has NO built-in 4096-char cap
# ---------------------------------------------------------------------------

def test_render_borderline_no_4096_cap_documented():
    """STRUCTURAL RISK ASSESSMENT: render_borderline has no message-length cap.

    With pathological input (long company + title + URL) the output can exceed
    Telegram's 4096-char message limit, which would cause the send to fail
    silently or raise an exception in the bot. This test documents the risk by
    verifying that:
    (a) the function itself does NOT raise on large input (no defensive truncation),
    (b) the output CAN exceed 4096 chars (the risk is real, not theoretical).

    Severity: MEDIUM. In practice, borderline items have short fields, so this
    is unlikely to trigger in normal operation. However, the bot has no defense
    against it. The fix would be to add message splitting or truncation in
    handle_borderline (e.g. send first 4096 chars then a continuation).
    """
    class _LargeItem:
        def __init__(self, n):
            self.relevance_score = 50.0 + (n % 10)
            self.extracted_json = json.dumps({
                "company": "A" * 80,
                "title": "B" * 80,
            })
            self.source_link = "https://t.me/longchannel/" + "x" * 60

    items = [_LargeItem(n) for n in range(30)]
    result = bot.render_borderline(items)

    # FIXED: render_borderline now caps the output under Telegram's 4096-char
    # limit and notes the remainder instead of overflowing.
    assert isinstance(result, str)
    assert len(result) <= 4096, (
        f"borderline output must stay <=4096 chars; got {len(result)}"
    )
    assert "…и ещё" in result, "a truncated band must note the remaining count"


# ---------------------------------------------------------------------------
# 7. advance_by_id / run_to_gate not called — spy proof
# ---------------------------------------------------------------------------

def test_handle_borderline_advance_not_called_spy(conn, deps, monkeypatch):
    """Additional spy: advance_by_id and run_to_gate are NOT invoked at all."""
    _seed(conn, score=55.0, msg_id=601)

    advance_calls = {"n": 0}
    run_to_gate_calls = {"n": 0}
    update_state_calls = {"n": 0}

    monkeypatch.setattr(
        pipeline, "advance_by_id",
        lambda *a, **k: advance_calls.__setitem__("n", advance_calls["n"] + 1),
    )
    monkeypatch.setattr(
        pipeline, "run_to_gate",
        lambda *a, **k: run_to_gate_calls.__setitem__("n", run_to_gate_calls["n"] + 1),
    )
    monkeypatch.setattr(
        store, "update_state",
        lambda *a, **k: update_state_calls.__setitem__("n", update_state_calls["n"] + 1),
    )

    b = bot.JobHunterBot(_cfg(), conn, deps)
    asyncio.run(b.handle_borderline(FakeAnswerMessage()))

    assert advance_calls["n"] == 0, "advance_by_id must NOT be called by handle_borderline"
    assert run_to_gate_calls["n"] == 0, "run_to_gate must NOT be called by handle_borderline"
    assert update_state_calls["n"] == 0, "store.update_state must NOT be called by handle_borderline"


# ---------------------------------------------------------------------------
# 8. Ordering: highest-score-first with float scores
# ---------------------------------------------------------------------------

def test_store_band_ordering_float_scores_highest_first(conn):
    """Band query must order by relevance_score DESC (highest-first)
    even when the scores are non-integer floats."""
    for score, mid in [(50.0, 1), (59.9, 2), (55.5, 3)]:
        iid = store.insert_item(conn, raw_text="x", source_channel="@c",
                                source_message_id=str(mid + 700))
        conn.execute("UPDATE work_items SET relevance_score = %s WHERE id = %s",
                     (score, iid))
    conn.commit()

    items = store.list_pipeline(conn, min_score=50.0, max_score=60.0)
    scores = [it.relevance_score for it in items]
    assert scores == sorted(scores, reverse=True), (
        f"Band items must be ordered highest-score-first; got {scores}"
    )
    assert scores[0] == pytest.approx(59.9)
    assert scores[-1] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# 9. API band with state-agnostic items across states
# ---------------------------------------------------------------------------

def test_api_band_state_agnostic(client, conn):
    """API band query must return items in any state when score is in [50, 60)."""
    for state, mid in [(REJECTED, 1), (SKIPPED, 2), (SURFACED, 3)]:
        _seed(conn, score=55.0, msg_id=mid + 800, state=state,
              company=f"Co_{state}", title="Job")

    # Out-of-band item at score 70
    _seed(conn, score=70.0, msg_id=9991, state=SURFACED, company="HighCo", title="High")

    data = client.get("/api/pipeline", params={"min_score": 50, "max_score": 60}).json()
    companies = {x["company"] for x in data}

    assert len(data) == 3, f"Expected 3 band items; got {len(data)}"
    assert "HighCo" not in companies, ">=60 item must not be in band result"


# ---------------------------------------------------------------------------
# 10. Empty band reply exact string and link preview
# ---------------------------------------------------------------------------

def test_handle_borderline_empty_link_preview_disabled(conn, deps):
    """Empty band reply also has link preview disabled."""
    b = bot.JobHunterBot(_cfg(), conn, deps)
    msg = FakeAnswerMessage()
    asyncio.run(b.handle_borderline(msg))

    reply = msg.replies[0]
    assert reply["text"] == bot.BORDERLINE_EMPTY
    lpo = reply.get("link_preview_options")
    assert lpo is not None and lpo.is_disabled is True, (
        "Even the empty-band reply must have link_preview_options(is_disabled=True)"
    )
