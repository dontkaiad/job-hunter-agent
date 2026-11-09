"""The harvest's scoring pass runs on a worker thread: the event loop stays free, the worker uses its
own connection, and a human acting on the same item mid-run wins (optimistic lock)."""
from __future__ import annotations

import asyncio
import threading
import time

from job_hunter import pipeline, run as run_mod, store
from job_hunter.config import Config
from job_hunter.pipeline import AdvanceResult, Deps


def test_process_items_keeps_the_event_loop_responsive_and_uses_its_own_connection(conn, pg_dsn, monkeypatch):
    seen = {}

    def slow_run_to_gate(c, item_id, deps=None, max_steps=10):
        seen["thread"] = threading.current_thread()
        seen["conn"] = c
        time.sleep(0.6)                                   # a synchronous model call
        return [AdvanceResult("moved", item_id, "scored", "surfaced")]

    monkeypatch.setattr(pipeline, "run_to_gate", slow_run_to_gate)
    cfg = Config(database_url=pg_dsn)

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        t = asyncio.create_task(ticker())
        out = await run_mod.process_items(cfg, conn, [7], Deps())
        t.cancel()
        return out, ticks

    out, ticks = asyncio.run(main())
    assert out == [7]
    assert ticks >= 5, "the loop kept running while the pass was blocked in a worker thread"
    assert seen["thread"] is not threading.main_thread()
    assert seen["conn"] is not conn, "the worker must not share the loop's connection"


def test_process_items_runs_inline_without_a_database_url(conn, monkeypatch):
    seen = {}

    def rtg(c, item_id, deps=None, max_steps=10):
        seen["conn"], seen["thread"] = c, threading.current_thread()
        return [AdvanceResult("moved", item_id, "scored", "surfaced")]

    monkeypatch.setattr(pipeline, "run_to_gate", rtg)
    out = asyncio.run(run_mod.process_items(Config(), conn, [1, 2], Deps()))
    assert out == [1, 2] and seen["conn"] is conn and seen["thread"] is threading.main_thread()


def test_threaded_pass_end_to_end_and_a_human_skip_during_it_wins(conn, pg_dsn, fake_llm, fake_fx, monkeypatch):
    post = ("Remote Senior Python engineer building LLM RAG agents with Claude API and FastAPI. "
            "Salary 250000-350000 RUB. Contact @hr_acme")
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False, min_persist_score=25)
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 85, "Обоснование": "good"}')
    a = store.insert_item(conn, raw_text=post, source_channel="@c", source_message_id="1")
    b = store.insert_item(conn, raw_text=post + " two", source_channel="@c", source_message_id="2")
    human = store.connect(pg_dsn)
    real = fake_llm.complete
    calls = {"n": 0}

    def complete(*args, **kw):
        out = real(*args, **kw)
        calls["n"] += 1
        if calls["n"] == 1:                               # first model call of the run: the operator skips item b
            human.execute("UPDATE work_items SET state='skipped' WHERE id=%s", (b,))
            human.commit()
        return out

    monkeypatch.setattr(fake_llm, "complete", complete)
    cfg = Config(database_url=pg_dsn)
    surfaced = asyncio.run(run_mod.process_items(cfg, conn, [a, b], deps))
    assert a in surfaced
    assert store.get_item(conn, b).state == "skipped", "the human's move stands"
    human.close()
