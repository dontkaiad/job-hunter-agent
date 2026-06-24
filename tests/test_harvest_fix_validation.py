"""Tester-added tests for the harvest re-delivery fix validation.

Validates all gaps identified in the Tester pass against the task specification:

1. BACKLOG NOT RE-DELIVERED (the core bug, adversarial angle):
   - Items in SURFACED state before the run are never passed to run_to_gate
     (to_process never contains SURFACED items; confirmed by run_to_gate call count).
   - A pure-SURFACED DB (no to-process items) results in run_to_gate called 0 times
     and notify_surfaced called 0 times.

2. MULTIPLE NEWLY-SURFACED + BACKLOG EXCLUDED:
   - When multiple items surface this run AND a backlog exists, ALL newly-surfaced
     are delivered and the backlog id is NOT delivered.

3. SUMMARY ORDERING:
   - The summary line is sent AFTER all per-item notify_surfaced calls (ordering verified
     via event log).
   - The summary is sent BEFORE aclose (lifecycle ordering).

4. COUNT CORRECTNESS (partial notify failure -> K < M in summary):
   - End-to-end: two items surface this run, one notify_surfaced raises.
     Summary must say "surfaced 2 · delivered 1" (K=1 < M=2).
     The failed id is NOT in the returned list. No retry.

5. HEARTBEAT ORDERING:
   - store.set_last_harvest_at is called after notify (verified via event ordering).
   - BEHAVIOR: if notify_text raises, set_last_harvest_at is NOT called (the heartbeat
     is at the very end of harvest; a failed summary send blocks it).
     This is an accepted edge case — report only.

6. run_to_gate DETECTION CORRECTNESS:
   - An approved→researched→drafted transition (T10→T11, no SURFACED crossing) in the
     run_to_gate results does NOT count as newly surfaced (no false positive).
   - A scored→surfaced (T4) transition IS counted.
   - An item that surfaces (T4) in run_to_gate is counted ONCE even if run_to_gate
     returns multiple steps (e.g., discovered→extracted→scored→surfaced in one call).

7. ADVERSARIAL: to_process never includes SURFACED items.
   - DB has only surfaced items (no discovered/extracted/scored/approved/researched).
     run_to_gate must be called ZERO times.

8. ADVERSARIAL: empty ingest + empty pipeline (N=0, M=0, K=0):
   - Summary still sent with "ingested 0 · surfaced 0 · delivered 0".
   - notify_surfaced called zero times.

All tests use real ephemeral PG (pg_dsn fixture) and mock ingest/LLM.
No production-logic changes — bugs are listed, not fixed.
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from job_hunter import run, store
from job_hunter.config import Config
from job_hunter.pipeline import AdvanceResult
from job_hunter.states import (
    APPROVED, DRAFTED, REJECTED, RESEARCHED, SCORED, SURFACED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(pg_dsn):
    return Config(
        bot_token="x",
        notify_chat_id=123,
        anthropic_api_key=None,
        allowed_user_ids={777},
        database_url=pg_dsn,
    )


def _seed_in_state(conn, state: str, n: int, *, start: int = 0) -> List[int]:
    """Insert n items and drive them to ``state`` via direct store updates."""
    import json

    ids = []
    for i in range(start, start + n):
        iid = store.insert_item(
            conn, raw_text=f"post {i}", source_channel="@c",
            source_message_id=f"sid_{state}_{i}"
        )
        ex_json = json.dumps({"title": f"post {i}", "stack": [], "reasons": []})
        if state in ("extracted", "scored", "surfaced", "rejected",
                     "approved", "researched", "drafted"):
            store.update_state(conn, iid, "extracted", from_state="discovered",
                               kind="deterministic", actor="system",
                               extracted_json=ex_json, relevance_score=80.0)
        if state in ("scored", "surfaced", "rejected", "approved",
                     "researched", "drafted"):
            store.update_state(conn, iid, "scored", from_state="extracted",
                               kind="deterministic", actor="system",
                               relevance_score=80.0)
        if state == "surfaced":
            store.update_state(conn, iid, "surfaced", from_state="scored",
                               kind="deterministic", actor="system")
        elif state == "rejected":
            store.update_state(conn, iid, "rejected", from_state="scored",
                               kind="deterministic", actor="system")
        elif state == "approved":
            store.update_state(conn, iid, "surfaced", from_state="scored",
                               kind="deterministic", actor="system")
            store.update_state(conn, iid, "approved", from_state="surfaced",
                               kind="hitl", actor="human")
        elif state == "researched":
            store.update_state(conn, iid, "surfaced", from_state="scored",
                               kind="deterministic", actor="system")
            store.update_state(conn, iid, "approved", from_state="surfaced",
                               kind="hitl", actor="human")
            store.update_state(conn, iid, "researched", from_state="approved",
                               kind="agent", actor="agent")
        ids.append(iid)
    return ids


# ---------------------------------------------------------------------------
# Gap 7 (adversarial): to_process NEVER includes SURFACED items
# When the DB contains only surfaced items, run_to_gate is called 0 times.
# ---------------------------------------------------------------------------

def test_to_process_never_includes_surfaced_state_items(monkeypatch, pg_dsn):
    """ADVERSARIAL: a DB that contains ONLY surfaced items (the standing backlog,
    no newly discoverd/extracted/scored/approved/researched items).

    The to_process loop iterates ('discovered','extracted','scored','approved',
    'researched') — NOT 'surfaced'. Therefore run_to_gate must be called ZERO
    times, and notify_surfaced must be called ZERO times.
    The summary must still be sent: 'ingested 0 · surfaced 0 · delivered 0'.
    """
    conn = store.connect(pg_dsn)
    store.init_db(conn)

    # Seed 3 items already in SURFACED — standing backlog.
    backlog_ids = _seed_in_state(conn, "surfaced", 3)

    gate_calls = {"n": 0}
    surfaced_calls = []
    texts = []

    async def fake_ingest(cfg, c):
        return []

    monkeypatch.setattr(run, "ingest", fake_ingest)

    def fake_run_to_gate(conn_, item_id, deps=None, **k):
        gate_calls["n"] += 1
        return []

    monkeypatch.setattr(run.pipeline, "run_to_gate", fake_run_to_gate)

    class FakeBot:
        async def notify_surfaced(self, item_id):
            surfaced_calls.append(item_id)

        async def notify_text(self, text):
            texts.append(text)

    asyncio.run(run.harvest(_cfg(pg_dsn), conn, FakeBot(), object()))

    # run_to_gate must NOT have been called (surfaced items not in to_process).
    assert gate_calls["n"] == 0, (
        f"run_to_gate must be called 0 times when DB has only surfaced items; "
        f"got {gate_calls['n']} — SURFACED items leaked into to_process"
    )
    # No per-item notifications.
    assert surfaced_calls == [], (
        f"notify_surfaced must NOT be called; got {surfaced_calls}"
    )
    # Summary still sent exactly once.
    assert len(texts) == 1
    assert texts[0] == "🟢 Harvest: ingested 0 · surfaced 0 · delivered 0"

    # Backlog items remain surfaced (not modified by harvest).
    still_surfaced = {it.id for it in store.list_by_state(conn, SURFACED)}
    assert set(backlog_ids) == still_surfaced
    conn.close()


# ---------------------------------------------------------------------------
# Gap 2: Multiple newly-surfaced + backlog excluded
# ---------------------------------------------------------------------------

def test_multiple_newly_surfaced_all_delivered_backlog_excluded(monkeypatch, pg_dsn):
    """Two items surface this run AND a pre-existing backlog exists.
    Both newly-surfaced ids must be delivered; the backlog id must NOT appear.
    Summary: 'surfaced 2 · delivered 2'.
    """
    conn = store.connect(pg_dsn)
    store.init_db(conn)

    backlog_ids = _seed_in_state(conn, "surfaced", 1, start=0)
    backlog_id = backlog_ids[0]
    scored_ids = _seed_in_state(conn, "scored", 3, start=10)  # 2 win, 1 loses

    winners = set(scored_ids[:2])
    loser = scored_ids[2]

    async def fake_ingest(cfg, c):
        return []

    monkeypatch.setattr(run, "ingest", fake_ingest)

    def fake_run_to_gate(conn_, item_id, deps=None, **k):
        if item_id in winners:
            store.update_state(conn_, item_id, "surfaced", from_state="scored",
                               kind="deterministic", actor="system")
            return [AdvanceResult("moved", item_id, SCORED, SURFACED, "T4")]
        store.update_state(conn_, item_id, "rejected", from_state="scored",
                           kind="deterministic", actor="system")
        return [AdvanceResult("moved", item_id, SCORED, REJECTED, "T3")]

    monkeypatch.setattr(run.pipeline, "run_to_gate", fake_run_to_gate)

    surfaced_calls = []
    texts = []

    class FakeBot:
        async def notify_surfaced(self, item_id):
            surfaced_calls.append(item_id)

        async def notify_text(self, text):
            texts.append(text)

    sent = asyncio.run(run.harvest(_cfg(pg_dsn), conn, FakeBot(), object()))

    # Both winners delivered; backlog NOT delivered.
    assert set(surfaced_calls) == winners, (
        f"Only winners must be delivered; got {surfaced_calls}"
    )
    assert backlog_id not in surfaced_calls, (
        f"Backlog id {backlog_id} must NOT be re-delivered"
    )
    assert set(sent) == winners

    # Summary exact.
    assert len(texts) == 1
    assert texts[0] == "🟢 Harvest: ingested 0 · surfaced 2 · delivered 2"

    # Backlog item still surfaced.
    still_surfaced = {it.id for it in store.list_by_state(conn, SURFACED)}
    assert backlog_id in still_surfaced
    # Winner items also now in surfaced.
    for w in winners:
        assert w in still_surfaced
    conn.close()


# ---------------------------------------------------------------------------
# Gap 4: COUNT CORRECTNESS — partial notify failure -> K < M in summary
# ---------------------------------------------------------------------------

def test_harvest_summary_reflects_partial_notify_failure(monkeypatch, pg_dsn):
    """COUNT CORRECTNESS (requirement 4): two items surface this run.
    One notify_surfaced raises. The summary must say 'surfaced 2 · delivered 1'
    (K=1 < M=2). The returned list contains only the successful id.
    The failed item is NOT retried (accepted edge; gather return_exceptions isolates).
    """
    conn = store.connect(pg_dsn)
    store.init_db(conn)

    scored_ids = _seed_in_state(conn, "scored", 2, start=200)
    winner_ok = scored_ids[0]
    winner_fail = scored_ids[1]

    async def fake_ingest(cfg, c):
        return []

    monkeypatch.setattr(run, "ingest", fake_ingest)

    def fake_run_to_gate(conn_, item_id, deps=None, **k):
        store.update_state(conn_, item_id, "surfaced", from_state="scored",
                           kind="deterministic", actor="system")
        return [AdvanceResult("moved", item_id, SCORED, SURFACED, "T4")]

    monkeypatch.setattr(run.pipeline, "run_to_gate", fake_run_to_gate)

    texts = []

    class PartialFailBot:
        async def notify_surfaced(self, item_id):
            if item_id == winner_fail:
                raise RuntimeError(f"deliberate notify failure for {item_id}")
            # OK for winner_ok

        async def notify_text(self, text):
            texts.append(text)

    sent = asyncio.run(run.harvest(_cfg(pg_dsn), conn, PartialFailBot(), object()))

    # Only the successful id is in returned list.
    assert sent == [winner_ok], (
        f"Only the successfully-notified id must be returned; got {sent}"
    )
    assert winner_fail not in sent

    # Summary reflects K=1 < M=2: exactly one notify_text, correct counts.
    assert len(texts) == 1, (
        f"Exactly one summary line must be sent; got {len(texts)}: {texts}"
    )
    # M (surfaced this run) = 2, K (delivered) = 1.
    assert "surfaced 2" in texts[0], (
        f"Summary must report surfaced 2 (M=2); got {texts[0]!r}"
    )
    assert "delivered 1" in texts[0], (
        f"Summary must report delivered 1 (K=1, one failed); got {texts[0]!r}"
    )
    # Full format check.
    assert texts[0] == "🟢 Harvest: ingested 0 · surfaced 2 · delivered 1", (
        f"Unexpected summary: {texts[0]!r}"
    )
    conn.close()


# ---------------------------------------------------------------------------
# Gap 3: SUMMARY ORDERING — summary sent AFTER per-item notifies
# ---------------------------------------------------------------------------

def test_summary_sent_after_per_item_notifies(monkeypatch, pg_dsn):
    """ORDER: the summary (notify_text) must be sent AFTER all per-item
    notify_surfaced calls. Verified via an ordered event log.
    """
    conn = store.connect(pg_dsn)
    store.init_db(conn)

    scored_ids = _seed_in_state(conn, "scored", 2, start=300)

    async def fake_ingest(cfg, c):
        return []

    monkeypatch.setattr(run, "ingest", fake_ingest)

    def fake_run_to_gate(conn_, item_id, deps=None, **k):
        store.update_state(conn_, item_id, "surfaced", from_state="scored",
                           kind="deterministic", actor="system")
        return [AdvanceResult("moved", item_id, SCORED, SURFACED, "T4")]

    monkeypatch.setattr(run.pipeline, "run_to_gate", fake_run_to_gate)

    events = []

    class OrderedBot:
        async def notify_surfaced(self, item_id):
            events.append(("per_item", item_id))

        async def notify_text(self, text):
            events.append(("summary", text))

    asyncio.run(run.harvest(_cfg(pg_dsn), conn, OrderedBot(), object()))

    # Verify structure: all per_item events before the summary.
    per_item_indices = [i for i, (k, _) in enumerate(events) if k == "per_item"]
    summary_indices = [i for i, (k, _) in enumerate(events) if k == "summary"]

    assert len(summary_indices) == 1, (
        f"Exactly one summary event expected; got {summary_indices} in {events}"
    )
    assert len(per_item_indices) == 2, (
        f"Exactly 2 per-item events expected; got {per_item_indices} in {events}"
    )
    summary_idx = summary_indices[0]
    assert all(pi < summary_idx for pi in per_item_indices), (
        f"All per-item notifies must precede the summary; events={events}"
    )
    conn.close()


# ---------------------------------------------------------------------------
# Gap 5: HEARTBEAT ORDERING — set_last_harvest_at called after notify
# ---------------------------------------------------------------------------

def test_heartbeat_written_after_notify_completes(monkeypatch, pg_dsn):
    """ORDER / HEARTBEAT: store.set_last_harvest_at must be called AFTER
    notify completes (after both per-item sends and the summary line).
    Verified via an event log that captures notify_text and the heartbeat write.
    """
    conn = store.connect(pg_dsn)
    store.init_db(conn)

    scored_ids = _seed_in_state(conn, "scored", 1, start=400)

    async def fake_ingest(cfg, c):
        return []

    monkeypatch.setattr(run, "ingest", fake_ingest)

    def fake_run_to_gate(conn_, item_id, deps=None, **k):
        store.update_state(conn_, item_id, "surfaced", from_state="scored",
                           kind="deterministic", actor="system")
        return [AdvanceResult("moved", item_id, SCORED, SURFACED, "T4")]

    monkeypatch.setattr(run.pipeline, "run_to_gate", fake_run_to_gate)

    events = []

    class TrackingBot:
        async def notify_surfaced(self, item_id):
            events.append(("per_item", item_id))

        async def notify_text(self, text):
            events.append(("summary", text))

    # Spy on store.set_last_harvest_at.
    original_set = store.set_last_harvest_at

    def spy_set_last_harvest_at(conn_, dt, **k):
        events.append(("heartbeat",))
        original_set(conn_, dt, **k)

    monkeypatch.setattr(store, "set_last_harvest_at", spy_set_last_harvest_at)

    asyncio.run(run.harvest(_cfg(pg_dsn), conn, TrackingBot(), object()))

    per_item_indices = [i for i, (k, *_) in enumerate(events) if k == "per_item"]
    summary_indices = [i for i, (k, *_) in enumerate(events) if k == "summary"]
    heartbeat_indices = [i for i, (k, *_) in enumerate(events) if k == "heartbeat"]

    assert len(heartbeat_indices) == 1, (
        f"Exactly one heartbeat write expected; events={events}"
    )
    assert len(summary_indices) == 1
    heartbeat_idx = heartbeat_indices[0]
    summary_idx = summary_indices[0]

    # Heartbeat after summary.
    assert heartbeat_idx > summary_idx, (
        f"Heartbeat must be written after the summary; events={events}"
    )
    # Heartbeat after all per-item notifies.
    assert all(pi < heartbeat_idx for pi in per_item_indices), (
        f"Heartbeat must follow all per-item notifies; events={events}"
    )
    conn.close()


# ---------------------------------------------------------------------------
# Gap 5b: notify_text failure -> heartbeat NOT written (behavior report)
# ---------------------------------------------------------------------------

def test_notify_text_failure_does_not_block_heartbeat(monkeypatch, pg_dsn):
    """FIXED: the summary send is BEST-EFFORT. If notify_text raises (Telegram
    hiccup), harvest does NOT propagate the exception and the heartbeat IS still
    written — the harvest body (ingest + score + per-item notify) completed, so
    the staleness watchdog must not fire a spurious "harvest hasn't run" alert
    just because the cosmetic summary failed.
    """
    conn = store.connect(pg_dsn)
    store.init_db(conn)

    async def fake_ingest(cfg, c):
        return []

    monkeypatch.setattr(run, "ingest", fake_ingest)
    monkeypatch.setattr(run.pipeline, "run_to_gate", lambda *a, **k: [])

    heartbeat_written = {"n": 0}
    original_set = store.set_last_harvest_at

    def spy_set(conn_, dt, **k):
        heartbeat_written["n"] += 1
        original_set(conn_, dt, **k)

    monkeypatch.setattr(store, "set_last_harvest_at", spy_set)

    class FailSummaryBot:
        async def notify_surfaced(self, item_id):
            pass

        async def notify_text(self, text):
            raise RuntimeError("summary send failed")

    # Harvest must NOT raise — the failed summary is swallowed (best-effort).
    sent = asyncio.run(run.harvest(_cfg(pg_dsn), conn, FailSummaryBot(), object()))
    assert sent == []

    # Heartbeat IS written despite the summary-send failure.
    assert heartbeat_written["n"] == 1, (
        f"When notify_text raises, the summary is best-effort and set_last_harvest_at "
        f"must STILL be called so the staleness watchdog doesn't fire a spurious alert; "
        f"got {heartbeat_written['n']} calls."
    )
    conn.close()


# ---------------------------------------------------------------------------
# Gap 6: run_to_gate detection — T4 fires, approved->researched->drafted does NOT
# ---------------------------------------------------------------------------

def test_run_to_gate_detection_t4_fires_not_t10_t11(monkeypatch, pg_dsn):
    """run_to_gate DETECTION CORRECTNESS: the SURFACED detection correctly fires
    for a scored->surfaced (T4) step and does NOT fire for approved->researched->drafted.

    Two scored items are set up:
    - item_a: run_to_gate returns [discovered->extracted, extracted->scored, scored->surfaced]
      (multi-step result; T4 is the last). Counted as newly surfaced.
    - item_b: run_to_gate returns [approved->researched, researched->drafted].
      No SURFACED crossing. NOT counted as newly surfaced (no false positive).
    """
    conn = store.connect(pg_dsn)
    store.init_db(conn)

    scored_ids = _seed_in_state(conn, "scored", 2, start=500)
    item_a, item_b = scored_ids

    async def fake_ingest(cfg, c):
        return []

    monkeypatch.setattr(run, "ingest", fake_ingest)

    def fake_run_to_gate(conn_, item_id, deps=None, **k):
        if item_id == item_a:
            # Multi-step chain ending in T4 (scored -> surfaced).
            store.update_state(conn_, item_id, "surfaced", from_state="scored",
                               kind="deterministic", actor="system")
            return [
                AdvanceResult("moved", item_id, "discovered", "extracted", "T1"),
                AdvanceResult("moved", item_id, "extracted", SCORED, "T2"),
                AdvanceResult("moved", item_id, SCORED, SURFACED, "T4"),
            ]
        if item_id == item_b:
            # T10 and T11 chain: no SURFACED state anywhere.
            return [
                AdvanceResult("moved", item_id, APPROVED, RESEARCHED, "T10"),
                AdvanceResult("moved", item_id, RESEARCHED, DRAFTED, "T11"),
            ]
        return []

    monkeypatch.setattr(run.pipeline, "run_to_gate", fake_run_to_gate)

    surfaced_calls = []
    texts = []

    class FakeBot:
        async def notify_surfaced(self, item_id):
            surfaced_calls.append(item_id)

        async def notify_text(self, text):
            texts.append(text)

    sent = asyncio.run(run.harvest(_cfg(pg_dsn), conn, FakeBot(), object()))

    # Only item_a counted as newly surfaced (T4 in its chain).
    assert surfaced_calls == [item_a], (
        f"Only item_a must be notified (T4 fires); got {surfaced_calls}"
    )
    assert item_b not in surfaced_calls, (
        "item_b (T10+T11, no SURFACED crossing) must NOT be counted as newly surfaced"
    )
    assert sent == [item_a]

    # Summary: M=1, K=1.
    assert len(texts) == 1
    assert texts[0] == "🟢 Harvest: ingested 0 · surfaced 1 · delivered 1"
    conn.close()


def test_run_to_gate_detection_item_counted_once_even_multistep(monkeypatch, pg_dsn):
    """An item that traverses discovered->extracted->scored->surfaced in a SINGLE
    run_to_gate call (4 AdvanceResult entries, one with to_state==SURFACED) must
    be counted as newly surfaced EXACTLY ONCE (the `any()` condition).
    """
    conn = store.connect(pg_dsn)
    store.init_db(conn)

    scored_ids = _seed_in_state(conn, "scored", 1, start=600)
    item_id = scored_ids[0]

    async def fake_ingest(cfg, c):
        return []

    monkeypatch.setattr(run, "ingest", fake_ingest)

    def fake_run_to_gate(conn_, iid, deps=None, **k):
        store.update_state(conn_, iid, "surfaced", from_state="scored",
                           kind="deterministic", actor="system")
        # Four steps; SURFACED appears only in the last.
        return [
            AdvanceResult("moved", iid, "discovered", "extracted", "T1"),
            AdvanceResult("moved", iid, "extracted", SCORED, "T2"),
            AdvanceResult("moved", iid, SCORED, SURFACED, "T4"),
        ]

    monkeypatch.setattr(run.pipeline, "run_to_gate", fake_run_to_gate)

    surfaced_calls = []

    class FakeBot:
        async def notify_surfaced(self, iid):
            surfaced_calls.append(iid)

        async def notify_text(self, text):
            pass

    asyncio.run(run.harvest(_cfg(pg_dsn), conn, FakeBot(), object()))

    assert surfaced_calls.count(item_id) == 1, (
        f"Item must be notified exactly ONCE even when run_to_gate returns "
        f"multiple steps with one SURFACED crossing; got {surfaced_calls}"
    )
    conn.close()


# ---------------------------------------------------------------------------
# Gap 8: empty ingest + empty pipeline -> summary still sent (M=0, N=0, K=0)
# ---------------------------------------------------------------------------

def test_empty_ingest_empty_pipeline_summary_always_sent(monkeypatch, pg_dsn):
    """EMPTY INGEST + EMPTY PIPELINE: no items at all.
    N=0, M=0, K=0. Summary still sent exactly once; notify_surfaced never called.
    """
    conn = store.connect(pg_dsn)
    store.init_db(conn)

    async def fake_ingest(cfg, c):
        return []

    monkeypatch.setattr(run, "ingest", fake_ingest)

    gate_calls = {"n": 0}

    def fake_run_to_gate(conn_, item_id, deps=None, **k):
        gate_calls["n"] += 1
        return []

    monkeypatch.setattr(run.pipeline, "run_to_gate", fake_run_to_gate)

    surfaced_calls = []
    texts = []

    class FakeBot:
        async def notify_surfaced(self, item_id):
            surfaced_calls.append(item_id)

        async def notify_text(self, text):
            texts.append(text)

    sent = asyncio.run(run.harvest(_cfg(pg_dsn), conn, FakeBot(), object()))

    assert gate_calls["n"] == 0  # no items to process
    assert surfaced_calls == []
    assert sent == []
    assert len(texts) == 1
    assert texts[0] == "🟢 Harvest: ingested 0 · surfaced 0 · delivered 0"
    conn.close()


def test_ingest_yields_items_but_all_reject_summary_m_zero(monkeypatch, pg_dsn):
    """Ingest yields N>0 new ids but run_to_gate returns T3 (reject) for all.
    Summary must show 'ingested N · surfaced 0 · delivered 0'.
    notify_surfaced called zero times.
    """
    conn = store.connect(pg_dsn)
    store.init_db(conn)

    # Seed 2 scored items so the loop processes them.
    scored_ids = _seed_in_state(conn, "scored", 2, start=700)

    async def fake_ingest(cfg, c):
        return list(scored_ids)  # N=2

    monkeypatch.setattr(run, "ingest", fake_ingest)

    def fake_run_to_gate(conn_, item_id, deps=None, **k):
        store.update_state(conn_, item_id, "rejected", from_state="scored",
                           kind="deterministic", actor="system")
        return [AdvanceResult("moved", item_id, SCORED, REJECTED, "T3")]

    monkeypatch.setattr(run.pipeline, "run_to_gate", fake_run_to_gate)

    surfaced_calls = []
    texts = []

    class FakeBot:
        async def notify_surfaced(self, item_id):
            surfaced_calls.append(item_id)

        async def notify_text(self, text):
            texts.append(text)

    sent = asyncio.run(run.harvest(_cfg(pg_dsn), conn, FakeBot(), object()))

    assert surfaced_calls == []
    assert sent == []
    assert len(texts) == 1
    assert texts[0] == "🟢 Harvest: ingested 2 · surfaced 0 · delivered 0", (
        f"Unexpected summary when all reject: {texts[0]!r}"
    )
    conn.close()


# ---------------------------------------------------------------------------
# Gap 5c (scheduled == manual confirmation via serve._scheduled_harvest body)
# ---------------------------------------------------------------------------

def test_serve_scheduled_harvest_is_await_run_mod_harvest():
    """The body of serve._scheduled_harvest is literally `await run_mod.harvest(...)`.
    Confirmed by inspecting the code object's co_names for the name 'harvest'
    and verifying the serve module's run_mod attribute is the run module.

    This is a static code inspection; no real PG or network needed.
    """
    from job_hunter import serve as serve_mod
    from job_hunter import run as run_mod_ref
    import inspect

    # serve._amain defines `_scheduled_harvest` as a local async def.
    # We extract its source to verify it contains `run_mod.harvest`.
    source = inspect.getsource(serve_mod._amain)
    assert "run_mod.harvest" in source, (
        "serve._amain's _scheduled_harvest body must contain 'run_mod.harvest'; "
        f"source excerpt does not show it. Source:\n{source}"
    )
    assert "await run_mod.harvest" in source, (
        "The harvest call must be AWAITED in _scheduled_harvest; "
        f"a sync call would not run the harvest body. Source:\n{source}"
    )
    # serve.run_mod is the same module as job_hunter.run.
    assert serve_mod.run_mod is run_mod_ref, (
        "serve.run_mod must be the same module object as job_hunter.run"
    )
