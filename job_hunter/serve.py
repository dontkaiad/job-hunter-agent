"""Long-running SERVE entrypoint — handles inline-button presses.

``python -m job_hunter.run`` is a ONE-SHOT job: it ingests, scores, sends the
surfaced cards, and EXITS. Nothing stays alive afterwards, so the
``callback_query`` updates produced when the operator taps Approve / Backlog /
Skip on a delivered card are never received and the buttons appear to do
nothing.

This module is the missing long-running half. ``python -m job_hunter.serve``
starts aiogram long-polling and STAYS ALIVE, dispatching every button press
through ``JobHunterBot.handle_callback`` (which calls ``pipeline.advance`` — the
sole state writer — acks the spinner, edits the card to a final-state line, and
on Approve drives research+draft and sends the draft back for review).

Both processes share the SAME SQLite DB (``cfg.db_path``), so buttons on cards
delivered by ``run`` work here even though they run in different processes.

DB connection ownership (sqlite thread affinity)
------------------------------------------------
sqlite3 connections have thread affinity: a connection may only be used in the
thread that created it. ``serve`` therefore opens its OWN connection inside the
polling process/thread (here, in ``_amain`` which runs under ``asyncio.run`` on
this thread) and never shares it across processes. This mirrors the affinity
discipline already used by ``run.py``'s threaded ingest.

Lifecycle
---------
A single ``asyncio.run`` wraps startup -> polling -> graceful shutdown. The
aiogram HTTP session (``bot.aclose``) and the DB connection are closed in a
``finally`` so they always release exactly once, after polling stops (Ctrl-C /
SIGTERM cancels ``start_polling``).

    python -m job_hunter.serve     # long-running; handles button presses
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from . import store
from .bot import JobHunterBot, build_deps
from .config import Config, load_config


async def _amain(cfg: Config | None = None) -> None:
    """Open resources, start long-polling, tear down cleanly.

    Everything lives under one event loop (started by ``main``'s
    ``asyncio.run``). The DB connection is created HERE — in the polling
    thread — to respect sqlite thread affinity, and closed last in the
    ``finally`` alongside the aiogram session.
    """
    load_dotenv()
    cfg = cfg or load_config()
    # Fail fast with a clear message if the bot can't be served at all.
    cfg.require("bot_token", "notify_chat_id")

    conn = store.connect(cfg.db_path)
    store.init_db(conn)
    deps = build_deps(cfg)

    bot = JobHunterBot(cfg, conn, deps)
    print(f"[serve] long-polling started (db={cfg.db_path}); Ctrl-C to stop")
    try:
        # Blocks until cancelled (Ctrl-C / SIGTERM). Handles approve/backlog/
        # skip and draft->send callback_query updates for the whole lifetime.
        await bot.run()
    finally:
        # Release the aiogram HTTP session first (no "Unclosed client session"),
        # then the DB connection. Runs exactly once on any exit path.
        await bot.aclose()
        conn.close()
        print("[serve] stopped; session + DB connection closed")


def main() -> None:
    # ONE asyncio.run wrapping startup -> polling -> teardown. KeyboardInterrupt
    # propagates out of start_polling as a clean cancel; the finally still runs.
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        # asyncio.run already ran the finally (teardown) during cancellation.
        pass


if __name__ == "__main__":
    main()
