"""End-to-end ingest -> score -> surface runner.

Reads new posts from Telegram, drives each through the deterministic pipeline
(extract -> score -> reject|surface, with Haiku extract, Sonnet rubric score,
and the deterministic FX salary-floor guard), and
pushes surfaced candidates to the operator via the aiogram bot.

Ingestion defaults to the public t.me/s/ web reader (NO auth required). Set
INGEST_MODE=telethon to use the optional Telethon userbot fallback instead
(requires TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION).

    python -m job_hunter.run            # one-shot ingest + score + notify

The ingest -> score -> notify body lives in the reusable ``harvest`` coroutine,
which is called both here (one-shot) and by the daily scheduled job in
``job_hunter.serve`` — so the manual run and the scheduled harvest share EXACTLY
one implementation.
"""

from __future__ import annotations

import asyncio
from typing import List

from dotenv import load_dotenv

from . import clock, ingest_telegram, ingest_web, obs, pipeline, store
from .bot import JobHunterBot, build_deps, notify
from .config import Config, load_config
from .states import SURFACED


async def ingest(cfg: Config, conn) -> List[int]:
    """Dispatch ingestion by INGEST_MODE: 'web' (default) or 'telethon'.

    'web' uses the public t.me/s/ HTTP reader with no authentication. It runs
    synchronously (httpx) but is offloaded to a worker thread so the async
    caller is not blocked. Each thread owns its OWN database connection: the
    worker thread opens and closes its own psycopg connection via
    ``ingest_web.ingest_with_own_connection`` -- the main-thread ``conn`` is
    deliberately NOT passed across the thread boundary.

    'telethon' runs on the event loop (this thread) and therefore uses the
    main-thread ``conn`` directly -- no thread crossing.
    """
    mode = (cfg.ingest_mode or "web").lower()
    if mode == "telethon":
        return await ingest_telegram.ingest_async(cfg, conn)
    return await asyncio.to_thread(ingest_web.ingest_with_own_connection, cfg)


async def harvest(cfg: Config, conn, bot: JobHunterBot, deps) -> List[int]:
    """The SHARED ingest -> score -> notify pipeline body (no teardown).

    This is the single source of truth for the harvest path. It is called BOTH
    by ``run._amain`` (the one-shot ``python -m job_hunter.run``) AND by the
    daily scheduled job inside ``job_hunter.serve`` — so the manual one-shot and
    the scheduled harvest run EXACTLY the same logic, with no fork/duplication.

    It deliberately does NOT own the lifecycle of ``conn`` or ``bot``: the
    caller opens and closes them. ``run._amain`` builds short-lived resources
    and closes them in a ``finally``; ``serve`` passes its already-open
    connection + bot (same process/loop, so sqlite thread affinity is honoured)
    and keeps them alive for continued polling.

    Steps:
      1) Ingest new posts (web by default; telethon when INGEST_MODE=telethon).
      2) Drive every discovered/extracted/scored item to its gate via
         ``pipeline.run_to_gate`` (the deterministic state machine; advance() is
         still the sole writer).
      3) Notify the operator about freshly surfaced items. ``notify`` awaits
         EVERY send to completion (asyncio.gather) before returning — no
         fire-and-forget, nothing left in flight when control returns.

    Returns the list of item ids that were successfully delivered.
    """
    # 1) Ingest new posts (web by default; telethon when INGEST_MODE=telethon).
    new_ids: List[int] = await ingest(cfg, conn)
    print(f"[harvest] ingested {len(new_ids)} new items (mode={cfg.ingest_mode})")

    # 2) Drive every discovered/extracted/scored item to its gate.
    to_process = set(new_ids)
    for st in ("discovered", "extracted", "scored", "approved", "researched"):
        for item in store.list_by_state(conn, st):
            to_process.add(item.id)
    for item_id in sorted(to_process):
        pipeline.run_to_gate(conn, item_id, deps=deps)

    # 3) Notify operator about freshly surfaced items. ``notify`` awaits
    # EVERY send to completion (asyncio.gather) before returning -- no
    # fire-and-forget, nothing left in flight when we reach teardown.
    surfaced = store.list_by_state(conn, SURFACED)
    print(f"[harvest] {len(surfaced)} surfaced; notifying operator")
    sent = await notify(bot, [item.id for item in surfaced])
    print(f"[harvest] {len(sent)}/{len(surfaced)} cards delivered")

    # When the run surfaced/delivered ZERO cards, silence is ambiguous (did the
    # harvest even run?). Send ONE concise line to the SAME operator chat so a
    # completed-but-empty run is visible. When there ARE cards, the cards
    # themselves are the notification — do NOT also send this line. This adds
    # only a notification; advance() remains the sole state writer.
    if not sent:
        await bot.notify_text(
            f"🟢 Harvest done — 0 new vacancies (ingested {len(new_ids)})."
        )

    # Observability heartbeat: record that a harvest COMPLETED, at the very end
    # (after ingest->score->notify). The staleness watchdog in serve reads this
    # once a day and alerts if it goes stale. This is an ADDITIVE write to the
    # SEPARATE ops_heartbeat table — NOT work_items — so advance() remains the
    # sole writer of pipeline state. It does not alter the harvest flow above.
    store.set_last_harvest_at(conn, clock.now_utc())
    return sent


async def _amain() -> None:
    """The WHOLE one-shot pipeline under a single event loop: build resources,
    run the SHARED ``harvest`` body, tear down.

    Lifecycle contract (see the bug fixed here): the aiogram Bot opens an
    aiohttp ClientSession/connector on its first request. ``harvest`` AWAITS
    every card send to completion (``notify`` gathers and awaits them all) and
    only THEN do we close the bot's HTTP session -- inside a ``finally`` so it
    always runs, exactly once, after all sends are done. The DB connection is
    closed last. Everything happens inside one ``asyncio.run`` (in ``main``):
    the loop is never torn down while HTTP requests are still in flight.
    """
    load_dotenv()
    cfg = load_config()
    cfg.require("database_url")
    conn = store.connect(cfg.database_url)
    store.init_db(conn)
    deps = build_deps(cfg)

    bot = JobHunterBot(cfg, conn, deps)
    # Observability: surface silent warnings ("coroutine never awaited") and
    # unhandled loop exceptions to the ops channel during manual one-shot runs
    # too. Installed inside the running coroutine so the running loop is valid.
    obs.install_all(asyncio.get_running_loop())
    try:
        # The one-shot harvest is the SAME callable the scheduler fires.
        await harvest(cfg, conn, bot, deps)
    finally:
        # Teardown runs ONLY after all sends finished. Close the aiogram HTTP
        # session first (no "Unclosed client session"/"Unclosed connector"),
        # then the DB connection. FX/ingest use short-lived httpx.get() calls
        # (no persistent session held open across the run), so there is no
        # additional client session to close there.
        await bot.aclose()
        conn.close()


def main() -> None:
    # ONE asyncio.run wrapping the entire pipeline including notify+teardown.
    # Never create/close nested loops; never call asyncio.run twice.
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
