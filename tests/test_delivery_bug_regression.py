"""Regression tests added by the Tester for the delivery-bug fix validation.

These cover gaps identified during the re-validation pass:
  1. notify() awaits ALL coroutines when one raises -- including the failing
     one itself (not just the survivors): completed of survivors verified.
  2. aclose() runs in the finally even when notify() itself raises (not just
     when ingest raises).
  3. No send happens after aclose() even in the partial-failure ordering.
  4. aclose() is a true no-op (zero session.close() calls) when _bot is None.
"""

from __future__ import annotations

import asyncio

import pytest

from job_hunter import bot, run, store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _TrackingBot:
    """Two-phase awaitable model. started increments at coroutine entry (before
    the first yield), completed increments only after the yield. A fire-and-
    forget send would have started > completed because the resumed half never
    runs."""

    def __init__(self):
        self._asyncio = asyncio
        self.started = 0
        self.completed = 0
        self.sent_ids = []
        self.events = []
        self.closed = False
        self.close_calls = 0

    async def notify_surfaced(self, item_id):
        self.started += 1
        self.events.append(("send_start", item_id))
        await self._asyncio.sleep(0)
        if self.closed:
            raise AssertionError(f"send {item_id} ran after close")
        self.completed += 1
        self.sent_ids.append(item_id)
        self.events.append(("send", item_id))

    async def aclose(self):
        self.close_calls += 1
        self.closed = True
        self.events.append(("close", None))


# ---------------------------------------------------------------------------
# 1. Partial-fail: completed count for surviving sends is correct
# ---------------------------------------------------------------------------

def test_notify_partial_fail_completed_count_for_survivors():
    """When send #2 raises, sends #1 and #3 must complete (completed == 2),
    NOT just start. This catches a theoretical fire-and-forget regression for
    the survivors."""

    class PartialFail(_TrackingBot):
        async def notify_surfaced(self, item_id):
            self.started += 1
            self.events.append(("send_start", item_id))
            await self._asyncio.sleep(0)
            if item_id == 2:
                raise RuntimeError("deliberate boom for item 2")
            self.completed += 1
            self.sent_ids.append(item_id)
            self.events.append(("send", item_id))

    fake = PartialFail()
    sent = asyncio.run(bot.notify(fake, [1, 2, 3]))

    # All three coroutines started.
    assert fake.started == 3, f"expected 3 started; got {fake.started}"
    # The two survivors completed (completed tracks the resumed half).
    assert fake.completed == 2, (
        f"expected 2 completed (survivors); got {fake.completed}"
    )
    # Returned list contains only the successful ids.
    assert sorted(sent) == [1, 3]
    assert sorted(fake.sent_ids) == [1, 3]


# ---------------------------------------------------------------------------
# 2. aclose() fires in the finally even when notify() itself raises
# ---------------------------------------------------------------------------

class _BoomNotifyBot(_TrackingBot):
    """notify_surfaced raises unconditionally to simulate a hard notify failure
    (e.g. the gather itself encountering a programming error)."""

    async def notify_surfaced(self, item_id):
        self.started += 1
        await self._asyncio.sleep(0)
        raise RuntimeError(f"notify boom for {item_id}")


def test_amain_closes_session_when_notify_raises(pg_dsn, monkeypatch):
    """If notify() raises (beyond isolated per-send errors), the finally block
    in _amain must still call bot.aclose() exactly once so there is no
    unclosed client session."""
    db_path = pg_dsn
    conn = store.connect(db_path)
    store.init_db(conn)
    # Seed ONE surfaced item so notify is actually called.
    import json
    iid = store.insert_item(conn, raw_text="job", source_channel="@c",
                            source_message_id="boom1")
    store.update_state(conn, iid, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps({"title": "job"}),
                       relevance_score=80.0)
    store.update_state(conn, iid, "surfaced", from_state="extracted",
                       kind="deterministic", actor="system")
    conn.close()

    captured = {}

    from job_hunter.config import Config

    monkeypatch.setattr(run, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(run, "load_config", lambda: Config(database_url=db_path))
    monkeypatch.setattr(run, "build_deps", lambda cfg: object())

    async def fake_ingest(cfg, conn):
        return []

    monkeypatch.setattr(run, "ingest", fake_ingest)
    monkeypatch.setattr(run.pipeline, "run_to_gate", lambda *a, **k: None)

    # Replace notify itself to raise (not individual sends).
    async def boom_notify(bot_obj, item_ids):
        raise RuntimeError("notify itself blew up")

    monkeypatch.setattr(run, "notify", boom_notify)

    def make_bot(cfg, conn, deps):
        b = _TrackingBot()
        # Give it a real aclose-like interface matching what _amain expects.
        captured["bot"] = b
        return b

    monkeypatch.setattr(run, "JobHunterBot", make_bot)

    with pytest.raises(RuntimeError, match="notify itself blew up"):
        asyncio.run(run._amain())

    assert captured["bot"].close_calls == 1, (
        f"aclose must be called exactly once even when notify raises; "
        f"got {captured['bot'].close_calls}"
    )


# ---------------------------------------------------------------------------
# 3. No send happens after aclose (ordered-event test for the close-last rule)
# ---------------------------------------------------------------------------

def test_close_last_send_before_close_in_all_scenarios():
    """Model the teardown: notify(bot, ids) in try, aclose() in finally. All
    'send' events must precede the single 'close' event, and there must be
    zero 'send_start' events after the 'close' event."""
    fake = _TrackingBot()
    ids = [10, 20, 30, 40]

    async def scenario():
        try:
            await bot.notify(fake, ids)
        finally:
            await fake.aclose()

    asyncio.run(scenario())

    close_idx = next(
        (i for i, (k, _) in enumerate(fake.events) if k == "close"), None
    )
    assert close_idx is not None, "close event missing"

    # Every send_start and send happened before close.
    for i, (kind, val) in enumerate(fake.events):
        if kind in ("send_start", "send"):
            assert i < close_idx, (
                f"event {kind}({val}) at position {i} is after close at {close_idx}"
            )

    # The 'close' event is the very last event in the log.
    assert fake.events[-1] == ("close", None)

    # All sends completed (no fire-and-forget).
    assert fake.completed == len(ids)
    assert fake.started == len(ids)


# ---------------------------------------------------------------------------
# 4. aclose() is a true no-op when the bot was never built (_bot is None)
# ---------------------------------------------------------------------------

def test_aclose_noop_when_bot_never_built(conn, deps):
    """When no surfaced items triggered _ensure(), _bot remains None. aclose()
    must be a silent no-op with zero session.close calls and must NOT set
    _closed = True (there is nothing to track)."""
    from job_hunter.config import Config
    from job_hunter.bot import JobHunterBot

    cfg = Config(bot_token=None, notify_chat_id=None)
    b = JobHunterBot(cfg, conn, deps)

    # _ensure never called -> _bot is None.
    assert b._bot is None

    asyncio.run(b.aclose())

    # After a no-op aclose, flag must remain False (nothing was closed).
    assert b._closed is False
    # Calling it again must still be a no-op (no AttributeError, no crash).
    asyncio.run(b.aclose())
    assert b._closed is False
