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

from . import clock, ingest_telegram, obs, pipeline, sources, store
from .bot import JobHunterBot, build_deps, notify
from .config import Config, load_config
from .states import SURFACED


async def ingest(cfg: Config, conn) -> List[int]:
    """Multi-source aggregator: fan out over every enabled Source, concat ids.

    Sources (see ``sources.py``):
      - Telegram: INGEST_MODE=web (default) uses the public t.me/s/ reader; set
        INGEST_MODE=telethon for the userbot fallback. The telethon path runs on
        the event loop (this thread) against the main-thread ``conn``; the web
        path is a synchronous Source offloaded to a worker thread.
      - Jobicy: the international-remote JSON API, active when JOBICY_GEOS is set.

    Each SYNCHRONOUS (httpx) Source is offloaded via ``asyncio.to_thread`` and
    owns its OWN psycopg connection end-to-end (``ingest_with_own_connection``);
    the main-thread ``conn`` is deliberately NOT crossed over the thread boundary.
    """
    mode = (cfg.ingest_mode or "web").lower()
    new_ids: List[int] = []
    if mode == "telethon":
        new_ids.extend(await ingest_telegram.ingest_async(cfg, conn))
    for source in sources.http_sources(cfg, mode=mode):
        new_ids.extend(
            await asyncio.to_thread(source.ingest_with_own_connection, cfg)
        )
    return new_ids


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
      3) Notify the operator about ONLY the items freshly surfaced THIS run
         (derived from the run_to_gate AdvanceResults — the standing surfaced
         backlog is NOT re-pushed), then ALWAYS send one summary line.
         ``notify`` awaits EVERY send to completion (asyncio.gather) before
         returning — no fire-and-forget, nothing left in flight when control
         returns.

    Returns the list of item ids that were successfully delivered.
    """
    # 1) Ingest new posts (web by default; telethon when INGEST_MODE=telethon).
    new_ids: List[int] = await ingest(cfg, conn)
    print(f"[harvest] ingested {len(new_ids)} new items (mode={cfg.ingest_mode})")

    # 2) Drive every discovered/extracted/scored item to its gate AND record
    # which items were freshly surfaced THIS run. ``run_to_gate`` already
    # returns the List[AdvanceResult] for every step it took; an item that
    # crossed the scored->surfaced gate (T4) yields a result with
    # ``to_state == SURFACED``. We collect exactly those ids.
    #
    # The to_process set is built from discovered/extracted/scored/approved/
    # researched ONLY -- NEVER from the standing SURFACED backlog. So items that
    # were already surfaced before this run are not iterated here, are not in
    # ``newly_surfaced``, and are therefore not re-notified (the re-delivery
    # bug). They remain in SURFACED in the DB (visible to the bot/dashboard);
    # advance() remains the sole state writer. "Newly surfaced this run" is
    # derived from these results, NOT persisted.
    to_process = set(new_ids)
    for st in ("discovered", "extracted", "scored", "approved", "researched"):
        for item in store.list_by_state(conn, st):
            to_process.add(item.id)
    newly_surfaced: List[int] = []
    for item_id in sorted(to_process):
        results = pipeline.run_to_gate(conn, item_id, deps=deps)
        if any(r.to_state == SURFACED for r in results):
            newly_surfaced.append(item_id)

    # 3) Notify the operator about ONLY the items freshly surfaced THIS run.
    # ``notify`` awaits EVERY send to completion (asyncio.gather) before
    # returning -- no fire-and-forget, nothing left in flight at teardown.
    print(f"[harvest] {len(newly_surfaced)} newly surfaced; notifying operator")
    sent = await notify(bot, newly_surfaced)
    print(f"[harvest] {len(sent)}/{len(newly_surfaced)} delivered")

    # ALWAYS send ONE concise summary line (scheduled AND manual), so a
    # completed run is always visible -- even one that surfaced/delivered ZERO
    # cards (silence would otherwise be ambiguous: did the harvest even run?).
    # This replaces the old conditional "0 new vacancies" line; the cards
    # themselves are still the per-item notification, this is the run summary.
    # It adds only a notification; advance() remains the sole state writer.
    # BEST-EFFORT: the summary is cosmetic — a failed send (Telegram hiccup) must
    # NOT crash the harvest NOR skip the heartbeat below (otherwise the staleness
    # watchdog would fire a spurious "harvest hasn't run" alert for a run that
    # actually completed). Swallow + log; the harvest body already succeeded.
    try:
        await bot.notify_text(
            f"🟢 Harvest: ingested {len(new_ids)} · surfaced {len(newly_surfaced)} · delivered {len(sent)}"
        )
    except Exception as exc:  # noqa: BLE001 - summary is non-critical
        print(f"[harvest] summary send failed (non-fatal): {exc!r}")

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
