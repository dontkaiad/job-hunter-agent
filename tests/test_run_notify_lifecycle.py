"""run._amain lifecycle: surfaced cards are AWAITED to completion and the bot
HTTP session is closed LAST (in a finally), under a single asyncio.run.

This is the regression test for the delivery bug where surfaced cards were
logged ("N surfaced; notifying operator") but never delivered, and teardown
threw "Unclosed client session" / "Unclosed connector" / "Event loop is
closed" -- because the aiogram session was never closed and the loop was torn
down with HTTP requests still in flight.

No real network / aiogram: the JobHunterBot and the ingest path are mocked.
"""

from __future__ import annotations

import asyncio

from job_hunter import run, store
from job_hunter.pipeline import AdvanceResult
from job_hunter.states import SCORED, SURFACED


class FakeBot:
    """Stand-in for JobHunterBot in run._amain. Records send order vs close."""

    def __init__(self, cfg, conn, deps):
        self.cfg = cfg
        self.conn = conn
        self.deps = deps
        self.events = []
        self.started = 0
        self.completed = 0
        self.close_calls = 0
        self.closed = False

    async def notify_surfaced(self, item_id):
        self.started += 1
        self.events.append(("send_start", item_id))
        await asyncio.sleep(0)
        if self.closed:
            raise AssertionError(f"send for {item_id} after close")
        self.completed += 1
        self.events.append(("send", item_id))

    async def notify_text(self, text):
        # The always-on harvest summary line. Recorded so the ordering check
        # (all sends before close) still sees it inside the run, never after.
        if self.closed:
            raise AssertionError("summary sent after close")
        self.events.append(("summary", text))

    async def aclose(self):
        self.close_calls += 1
        self.closed = True
        self.events.append(("close", None))


def _seed_scored(conn, n):
    """Seed ``n`` SCORED items (the gate's INPUT state).

    They are left in SCORED so that, when ``run_to_gate`` is driven over them
    this run, the gate (mocked below) surfaces them -> they count as "newly
    surfaced this run" and are the ids harvest must notify. We are NOT testing
    the state machine here (run_to_gate is mocked); we only need the items to
    sit in a state the loop iterates over (scored).
    """
    import json

    ids = []
    for i in range(n):
        iid = store.insert_item(
            conn, raw_text=f"job {i}", source_channel="@c", source_message_id=str(i)
        )
        store.update_state(
            conn, iid, "extracted", from_state="discovered",
            kind="deterministic", actor="system",
            extracted_json=json.dumps({"title": f"job {i}"}), relevance_score=80.0,
        )
        store.update_state(conn, iid, "scored", from_state="extracted",
                           kind="deterministic", actor="system",
                           relevance_score=80.0)
        ids.append(iid)
    return ids


def test_amain_awaits_all_sends_then_closes_session(pg_dsn, monkeypatch):
    db_path = pg_dsn
    conn = store.connect(db_path)
    store.init_db(conn)
    ids = _seed_scored(conn, 4)
    conn.close()

    captured = {}

    # Patch the heavy pieces: config, ingest, pipeline, deps, and the Bot.
    from job_hunter.config import Config

    monkeypatch.setattr(run, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(run, "load_config", lambda: Config(database_url=db_path))
    monkeypatch.setattr(run, "build_deps", lambda cfg: object())

    async def fake_ingest(cfg, conn):
        return []

    monkeypatch.setattr(run, "ingest", fake_ingest)

    # The gate surfaces each SCORED item this run -> return a SCORED->SURFACED
    # AdvanceResult so harvest counts it as newly surfaced and notifies it.
    def fake_run_to_gate(conn, item_id, deps=None, **k):
        return [AdvanceResult("moved", item_id, SCORED, SURFACED, "T4")]

    monkeypatch.setattr(run.pipeline, "run_to_gate", fake_run_to_gate)

    def make_bot(cfg, conn, deps):
        captured["bot"] = FakeBot(cfg, conn, deps)
        return captured["bot"]

    monkeypatch.setattr(run, "JobHunterBot", make_bot)

    asyncio.run(run._amain())

    bot = captured["bot"]
    # Every surfaced card was AWAITED to completion.
    assert bot.started == len(ids)
    assert bot.completed == len(ids) == bot.started
    # Session closed exactly once, and LAST (after all sends).
    assert bot.close_calls == 1
    assert bot.events[-1] == ("close", None)
    send_idx = [i for i, (k, _) in enumerate(bot.events) if k == "send"]
    close_idx = bot.events.index(("close", None))
    assert all(i < close_idx for i in send_idx)
    assert len(send_idx) == len(ids)


def test_amain_closes_session_even_on_error(pg_dsn, monkeypatch):
    """If the body raises, the finally must still close the bot session so
    there is never an unclosed client session at loop teardown."""
    db_path = pg_dsn
    conn = store.connect(db_path)
    store.init_db(conn)
    conn.close()

    captured = {}
    from job_hunter.config import Config

    monkeypatch.setattr(run, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(run, "load_config", lambda: Config(database_url=db_path))
    monkeypatch.setattr(run, "build_deps", lambda cfg: object())

    async def boom_ingest(cfg, conn):
        raise RuntimeError("ingest blew up")

    monkeypatch.setattr(run, "ingest", boom_ingest)

    def make_bot(cfg, conn, deps):
        captured["bot"] = FakeBot(cfg, conn, deps)
        return captured["bot"]

    monkeypatch.setattr(run, "JobHunterBot", make_bot)

    try:
        asyncio.run(run._amain())
    except RuntimeError:
        pass

    assert captured["bot"].close_calls == 1
