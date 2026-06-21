"""Long-running SERVE entrypoint — polling + a daily scheduled harvest.

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

In ADDITION to polling, ``serve`` runs an ``AsyncIOScheduler`` with a SINGLE
daily harvest job (cron, 10:00 in the operator's local / configured timezone)
that fires the SAME ingest -> score -> notify path as ``run.py`` via the shared
``run.harvest`` coroutine. So one container both delivers fresh cards each
morning AND handles the button taps continuously.

Both processes/paths share the SAME SQLite DB (``cfg.db_path``), so buttons on
cards delivered by ``run`` (or by the scheduled harvest) work here.

DB connection ownership (sqlite thread affinity)
------------------------------------------------
sqlite3 connections have thread affinity: a connection may only be used in the
thread that created it. ``serve`` opens its OWN connection inside the polling
process/thread (here, in ``_amain`` which runs under ``asyncio.run`` on this
thread). The scheduled harvest is SAFE to reuse this connection because
``AsyncIOScheduler`` runs the job coroutine on the SAME event loop / thread as
``serve`` — there is no thread crossing. (Web ingest itself offloads its sync
httpx work to a worker thread that opens its OWN connection, so that part keeps
its existing affinity discipline; serve's ``conn`` is only ever touched on the
loop thread.)

Timezone (scheduler trigger ONLY)
---------------------------------
The 10:00 trigger fires at 10:00 LOCAL time. The timezone is resolved from the
``SCHEDULE_TZ`` env var (an IANA name, e.g. ``Europe/Belgrade``); if unset it
falls back to the SYSTEM local timezone (never hardcoded UTC). This tz is used
ONLY to configure the cron trigger — it is NOT introduced into business logic.
All STORED timestamps still come from ``clock.now_utc`` (the sole now() site).

Lifecycle
---------
A single ``asyncio.run`` wraps startup -> (start scheduler, then await polling)
-> graceful shutdown. Polling blocks the coroutine for the whole lifetime while
the scheduler fires the harvest on the same loop. On shutdown (``finally``) the
order is: shut the scheduler down, then ``bot.aclose()``, then ``conn.close()``
— so no harvest can run against a closed session/connection.

    python -m job_hunter.serve     # long-running; polling + daily 10:00 harvest
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from . import run as run_mod
from . import store
from .bot import JobHunterBot, build_deps
from .config import Config, load_config

# Daily harvest time (LOCAL to the resolved schedule timezone).
HARVEST_HOUR = 10
HARVEST_MINUTE = 0


def resolve_schedule_tz(name: str | None):
    """Resolve the scheduler timezone from an env value. PURE given ``name``.

    - A non-empty IANA name (e.g. ``"Europe/Belgrade"``) -> ``ZoneInfo(name)``.
    - Empty / None -> the SYSTEM local timezone, obtained from the stdlib via
      ``datetime.now().astimezone().tzinfo`` (a fixed-offset/local tzinfo). This
      is NEVER hardcoded to UTC: on a box configured for Europe/Belgrade it
      yields that local offset; only a box whose clock IS UTC yields UTC.

    The returned object is a ``tzinfo`` suitable for an APScheduler cron trigger
    so the 10:00 fires at 10:00 local. It is used for triggering ONLY — stored
    timestamps still come from ``clock.now_utc``.
    """
    if name:
        cleaned = name.strip()
        if cleaned:
            return ZoneInfo(cleaned)
    # No SCHEDULE_TZ set: fall back to the system local timezone (stdlib, no
    # hardcoded UTC). astimezone() on a naive 'now' attaches the local tzinfo.
    return datetime.now().astimezone().tzinfo


def build_scheduler(harvest_coro_factory, tz):
    """Build an ``AsyncIOScheduler`` with EXACTLY one daily harvest job.

    Pure-ish factory: it constructs and CONFIGURES the scheduler but does NOT
    start it (so it is unit-testable without a running event loop). The caller
    starts it (``scheduler.start()``) once the loop is running.

    Args:
        harvest_coro_factory: a zero-arg callable returning a fresh coroutine to
            run on each fire (e.g. ``lambda: run.harvest(cfg, conn, bot, deps)``).
            APScheduler awaits the coroutine on the shared event loop.
        tz: the ``tzinfo`` for the cron trigger (from ``resolve_schedule_tz``),
            so 10:00 means 10:00 local.

    Returns the configured (NOT started) scheduler.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    # Set the scheduler's own timezone too so any tz-naive bookkeeping aligns
    # with the trigger; the trigger carries the authoritative tz for firing.
    scheduler = AsyncIOScheduler(timezone=tz)
    trigger = CronTrigger(hour=HARVEST_HOUR, minute=HARVEST_MINUTE, timezone=tz)
    scheduler.add_job(
        harvest_coro_factory,
        trigger=trigger,
        id="daily_harvest",
        name="daily_harvest",
        # Don't pile up missed runs if the box was asleep; just take the latest.
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )
    return scheduler


async def _amain(cfg: Config | None = None) -> None:
    """Open resources, start the scheduler, start long-polling, tear down.

    Everything lives under one event loop (started by ``main``'s
    ``asyncio.run``). The DB connection is created HERE — in the polling
    thread — to respect sqlite thread affinity, and is reused by the scheduled
    harvest (same loop/thread). Teardown closes the scheduler first, then the
    aiogram session, then the DB connection, in the ``finally``.
    """
    load_dotenv()
    cfg = cfg or load_config()
    # Fail fast with a clear message if the bot can't be served at all.
    cfg.require("bot_token", "notify_chat_id")

    conn = store.connect(cfg.db_path)
    store.init_db(conn)
    deps = build_deps(cfg)

    bot = JobHunterBot(cfg, conn, deps)

    # Resolve the schedule timezone (local default; never hardcoded UTC) and
    # build the scheduler with one daily 10:00-local harvest using the SHARED
    # run.harvest path. The factory makes a fresh coroutine on each fire.
    import os

    tz = resolve_schedule_tz(os.environ.get("SCHEDULE_TZ"))
    scheduler = build_scheduler(
        lambda: run_mod.harvest(cfg, conn, bot, deps),
        tz,
    )

    print(
        f"[serve] long-polling started (db={cfg.db_path}); "
        f"daily harvest at {HARVEST_HOUR:02d}:{HARVEST_MINUTE:02d} {tz}; Ctrl-C to stop"
    )
    try:
        # Start the scheduler FIRST (non-blocking: it just arms the loop timer),
        # then await polling which blocks for the whole lifetime. The scheduler
        # fires the harvest coroutine on this same loop while polling runs.
        scheduler.start()
        await bot.run()
    finally:
        # Shut the scheduler down BEFORE closing the bot session / DB, so no
        # harvest can start running against a closed session or connection.
        # wait=False: don't block teardown on an in-flight job during a Ctrl-C.
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            # Scheduler may never have started (e.g. cfg.require raised earlier
            # paths) — shutting a non-running scheduler must not mask teardown.
            pass
        # Release the aiogram HTTP session first (no "Unclosed client session"),
        # then the DB connection. Runs exactly once on any exit path.
        await bot.aclose()
        conn.close()
        print("[serve] stopped; scheduler + session + DB connection closed")


def main() -> None:
    # ONE asyncio.run wrapping startup -> scheduler+polling -> teardown.
    # KeyboardInterrupt propagates out of start_polling as a clean cancel; the
    # finally still runs.
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        # asyncio.run already ran the finally (teardown) during cancellation.
        pass


if __name__ == "__main__":
    main()
