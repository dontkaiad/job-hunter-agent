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

Both processes/paths share the SAME PostgreSQL DB (``cfg.database_url``), so buttons on
cards delivered by ``run`` (or by the scheduled harvest) work here.

DB connection ownership (psycopg thread affinity)
-------------------------------------------------
psycopg connections are not safe to share across threads concurrently, so
``serve`` opens its OWN connection inside the polling process/thread (here, in
``_amain`` which runs under ``asyncio.run`` on this thread). The scheduled
harvest is SAFE to reuse this connection because ``AsyncIOScheduler`` runs the
job coroutine on the SAME event loop / thread as ``serve`` — there is no thread
crossing. (Web ingest itself offloads its sync httpx work to a worker thread
that opens its OWN connection, so that part keeps its existing affinity
discipline; serve's ``conn`` is only ever touched on the loop thread.)

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
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from . import obs
from . import run as run_mod
from . import store
from .bot import JobHunterBot, build_deps
from .config import Config, load_config

# Daily harvest time (LOCAL to the resolved schedule timezone).
HARVEST_HOUR = 10
HARVEST_MINUTE = 0

# Harvest staleness watchdog runs the hour AFTER the harvest (11:00 local), once
# a day, so it sends AT MOST ONE alert per day (no spam, no extra dedup state).
STALENESS_CHECK_HOUR = HARVEST_HOUR + 1
STALENESS_CHECK_MINUTE = 0

# --- Heartbeat healthcheck (Part C) -----------------------------------------
# A background task writes the current epoch to HEARTBEAT_PATH every
# HEARTBEAT_INTERVAL_S seconds. The docker-compose healthcheck reads this file
# and marks the container unhealthy if it goes stale (> 90s old), which catches
# a wedged event loop that a "is the PID alive?" grep would miss.
HEARTBEAT_PATH_DEFAULT = "/tmp/heartbeat"
HEARTBEAT_INTERVAL_S = 30


def heartbeat_path() -> str:
    """Resolve the heartbeat file path (HEARTBEAT_PATH env, default /tmp)."""
    return os.environ.get("HEARTBEAT_PATH") or HEARTBEAT_PATH_DEFAULT


def write_heartbeat(path: str) -> None:
    """Write the current epoch (int seconds) to ``path``. NEVER raises.

    The whole body is guarded so a transient write error (full /tmp, perms)
    can never crash the heartbeat loop. Uses time.time() directly — this is a
    liveness epoch file, NOT a stored business timestamp, so it does not go
    through clock.now_utc.
    """
    try:
        with open(path, "w", encoding="ascii") as f:
            f.write(str(int(time.time())))
    except Exception:
        # Liveness file only; a failed write must not take down the loop.
        pass


async def heartbeat_loop(path: str, interval: float = HEARTBEAT_INTERVAL_S) -> None:
    """Write the heartbeat every ``interval`` seconds until cancelled.

    Writes once immediately (so the file exists right after startup, before the
    first interval elapses), then loops. Cancellation (on shutdown) propagates
    out cleanly via CancelledError.
    """
    write_heartbeat(path)
    while True:
        await asyncio.sleep(interval)
        write_heartbeat(path)


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


def build_scheduler(harvest_coro_func, tz):
    """Build an ``AsyncIOScheduler`` with EXACTLY one daily harvest job.

    Pure-ish factory: it constructs and CONFIGURES the scheduler but does NOT
    start it (so it is unit-testable without a running event loop). The caller
    starts it (``scheduler.start()``) once the loop is running.

    Args:
        harvest_coro_func: a COROUTINE FUNCTION (``async def``), zero-arg, that
            AWAITS the harvest on each fire (e.g. an ``async def`` wrapper that
            does ``await run.harvest(cfg, conn, bot, deps)``). It must be a
            coroutine function — ``asyncio.iscoroutinefunction`` must be True —
            NOT a plain sync function that merely RETURNS a coroutine.
            AsyncIOScheduler only AWAITS a job when the job func is itself a
            coroutine function; a sync function returning a coroutine has its
            return value discarded and the coroutine is NEVER awaited (the
            harvest body never executes). It is awaited on the shared running
            event loop — the SAME loop as aiogram polling — so there is no
            second loop and no ``asyncio.run`` inside the job.
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
        harvest_coro_func,
        trigger=trigger,
        id="daily_harvest",
        name="daily_harvest",
        # Don't pile up missed runs if the box was asleep; just take the latest.
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )
    return scheduler


def add_staleness_job(scheduler, staleness_coro_func, tz):
    """Add the once-daily harvest-staleness watchdog job to ``scheduler``.

    Registered as a SECOND job (id='harvest_staleness') on the SAME scheduler /
    loop, a daily cron at STALENESS_CHECK_HOUR (11:00 local — the hour after the
    harvest). Once-daily firing bounds it to AT MOST ONE alert per day, so no
    extra dedup state is needed. The existing daily_harvest job is untouched
    (its coalesce/max_instances/replace_existing are unchanged).

    ``staleness_coro_func`` MUST be a coroutine function (``async def``) so
    AsyncIOScheduler AWAITS it on the running loop — same discipline as the
    harvest job.
    """
    from apscheduler.triggers.cron import CronTrigger

    trigger = CronTrigger(
        hour=STALENESS_CHECK_HOUR, minute=STALENESS_CHECK_MINUTE, timezone=tz
    )
    scheduler.add_job(
        staleness_coro_func,
        trigger=trigger,
        id="harvest_staleness",
        name="harvest_staleness",
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
    cfg.require("bot_token", "notify_chat_id", "database_url")

    conn = store.connect(cfg.database_url)
    store.init_db(conn)
    deps = build_deps(cfg)

    bot = JobHunterBot(cfg, conn, deps)

    # Resolve the schedule timezone (local default; never hardcoded UTC) and
    # build the scheduler with one daily 10:00-local harvest using the SHARED
    # run.harvest path.
    #
    # The job MUST be a coroutine function (``async def``) so AsyncIOScheduler
    # AWAITS it. A plain sync lambda that RETURNS ``run_mod.harvest(...)`` (a
    # coroutine) would have its return value discarded and never awaited -> the
    # ingest->score->notify body would never run ("coroutine was never
    # awaited"). The async wrapper closes over the local cfg/conn/bot/deps and
    # awaits harvest on the EXISTING running loop (the same loop asyncio.run
    # below runs aiogram polling on) — no second event loop, no asyncio.run
    # inside the job, so serve's loop-thread ``conn`` keeps its thread affinity.
    async def _scheduled_harvest():
        await run_mod.harvest(cfg, conn, bot, deps)

    # SECOND scheduled job (observability): once a day, the hour after the
    # harvest, check whether the harvest heartbeat has gone stale and, if so,
    # send a LOUD ops alert. Read-only against the ops heartbeat table; it never
    # touches work_items, so advance() stays the sole pipeline-state writer.
    async def _scheduled_staleness_check():
        await obs.check_harvest_staleness(conn)

    tz = resolve_schedule_tz(os.environ.get("SCHEDULE_TZ"))
    scheduler = build_scheduler(_scheduled_harvest, tz)
    add_staleness_job(scheduler, _scheduled_staleness_check, tz)

    print(
        f"[serve] long-polling started (db={cfg.database_url}); "
        f"daily harvest at {HARVEST_HOUR:02d}:{HARVEST_MINUTE:02d} {tz}; Ctrl-C to stop"
    )
    # Heartbeat: a background task on THIS loop writes the liveness epoch file
    # every 30s. Created with asyncio.create_task BEFORE awaiting polling so it
    # runs concurrently under the SAME asyncio.run; cancelled in the finally.
    hb_path = heartbeat_path()
    hb_task = asyncio.create_task(heartbeat_loop(hb_path))

    # Observability hooks: promote silent Python warnings (notably the
    # "coroutine '...' was never awaited" RuntimeWarning) and unhandled
    # loop/task exceptions ("Task exception was never retrieved") to debounced
    # ops-channel alerts. Installed HERE — inside the running coroutine — so
    # ``asyncio.get_running_loop()`` is valid and the warning handler can hop
    # back onto THIS loop (warnings can fire off-loop from GC finalization).
    loop = asyncio.get_running_loop()
    obs.install_all(loop)

    try:
        # Start the scheduler FIRST (non-blocking: it just arms the loop timer),
        # then await polling which blocks for the whole lifetime. The scheduler
        # fires the harvest coroutine on this same loop while polling runs.
        scheduler.start()
        await bot.run()
    finally:
        # Stop the heartbeat task first (cancel + await its CancelledError) so it
        # is fully torn down alongside the scheduler/bot — no orphaned task.
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
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
