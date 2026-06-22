"""serve's scheduler + the shared harvest callable (deploy-infra addition).

Covers, with NO real network / no real scheduler-fired jobs / no real polling:

  * resolve_schedule_tz: SCHEDULE_TZ name -> ZoneInfo; empty -> SYSTEM local tz
    (never hardcoded UTC).
  * build_scheduler: registers EXACTLY ONE daily cron job, hour==10 minute==0,
    in the configured tz — asserted WITHOUT starting the scheduler or a loop.
  * The scheduled job calls the SAME shared path: serve wires
    ``run.harvest(cfg, conn, bot, deps)`` as the job, and run.main's one-shot
    drives the SAME ``run.harvest`` callable -> manual one-shot == scheduled
    harvest logic.
  * The shared ``run.harvest`` runs ingest -> run_to_gate -> notify (mocked).
  * serve._amain starts the scheduler and shuts it down in the finally BEFORE
    bot.aclose() + conn.close() (ordering asserted with a fake scheduler).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from job_hunter import bot as bot_mod
from job_hunter import run, serve, store
from job_hunter.config import Config


ALLOWED_UID = 777


def _cfg(**kw):
    base = dict(
        bot_token="x",
        notify_chat_id=123,
        anthropic_api_key=None,
        allowed_user_ids={ALLOWED_UID},
    )
    base.update(kw)
    return Config(**base)


# --- resolve_schedule_tz ----------------------------------------------------


def test_resolve_schedule_tz_named_returns_zoneinfo():
    tz = serve.resolve_schedule_tz("Europe/Belgrade")
    assert tz == ZoneInfo("Europe/Belgrade")


def test_resolve_schedule_tz_strips_whitespace():
    assert serve.resolve_schedule_tz("  Europe/Belgrade  ") == ZoneInfo("Europe/Belgrade")


def test_resolve_schedule_tz_empty_falls_back_to_system_local_not_hardcoded_utc():
    """Empty/None -> the SYSTEM local tz (the same value the stdlib helper
    yields), explicitly NOT a hardcoded UTC literal."""
    expected_local = datetime.now().astimezone().tzinfo
    assert serve.resolve_schedule_tz(None) == expected_local
    assert serve.resolve_schedule_tz("") == expected_local
    assert serve.resolve_schedule_tz("   ") == expected_local
    # Guard: it is the resolved-local helper's output, not the UTC literal.
    # (Only equal to UTC if the box itself is on UTC.)
    if expected_local != timezone.utc:
        assert serve.resolve_schedule_tz(None) != timezone.utc


# --- build_scheduler: exactly one daily 10:00 cron job, in the given tz ------


def _cron_field(trigger, name):
    """Read a single int value out of an APScheduler CronTrigger field."""
    for f in trigger.fields:
        if f.name == name:
            # A cron field holds expressions; for a fixed int it stringifies to
            # that int.
            return int(str(f))
    raise AssertionError(f"no cron field {name!r}")


def test_build_scheduler_registers_exactly_one_daily_10_00_job_in_tz():
    tz = ZoneInfo("Europe/Belgrade")

    async def harvest_func():
        pass

    scheduler = serve.build_scheduler(harvest_func, tz)

    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    trigger = job.trigger
    # hour==10, minute==0.
    assert _cron_field(trigger, "hour") == serve.HARVEST_HOUR == 10
    assert _cron_field(trigger, "minute") == serve.HARVEST_MINUTE == 0
    # Trigger timezone is exactly the configured tz.
    assert trigger.timezone == tz
    # The job function is the supplied coroutine function (shared-harvest wiring).
    assert job.func is harvest_func
    # It MUST be a coroutine function so AsyncIOScheduler awaits it.
    assert asyncio.iscoroutinefunction(job.func) is True
    # Built but NOT started (unit-testable without a running loop).
    assert scheduler.running is False


def test_build_scheduler_default_local_tz_when_unset():
    """With SCHEDULE_TZ unset, the resolved tz is the local tz (NOT hardcoded
    UTC unless the box is UTC), and the scheduler uses it."""
    tz = serve.resolve_schedule_tz(None)
    scheduler = serve.build_scheduler(lambda: None, tz)
    job = scheduler.get_jobs()[0]
    assert job.trigger.timezone == tz
    assert _cron_field(job.trigger, "hour") == 10
    assert _cron_field(job.trigger, "minute") == 0


# --- shared harvest callable: ingest -> run_to_gate -> notify ---------------


def test_run_harvest_runs_ingest_score_notify(monkeypatch, pg_dsn):
    """The shared run.harvest calls ingest, drives run_to_gate, and notifies the
    surfaced items — all mocked, no real network."""
    db_path = pg_dsn
    conn = store.connect(db_path)
    store.init_db(conn)

    import json

    # Seed two EXTRACTED items (so they enter the to_process set and run_to_gate
    # is exercised) AND two SURFACED items (so notify has something to deliver).
    # We are not testing the state machine here; run_to_gate is mocked.
    extracted_ids = []
    for i in range(2):
        iid = store.insert_item(
            conn, raw_text=f"pending {i}", source_channel="@c",
            source_message_id=f"p{i}",
        )
        store.update_state(
            conn, iid, "extracted", from_state="discovered",
            kind="deterministic", actor="system",
            extracted_json=json.dumps({"title": f"pending {i}"}), relevance_score=80.0,
        )
        extracted_ids.append(iid)

    ids = []
    for i in range(2):
        iid = store.insert_item(
            conn, raw_text=f"job {i}", source_channel="@c", source_message_id=str(i)
        )
        store.update_state(
            conn, iid, "extracted", from_state="discovered",
            kind="deterministic", actor="system",
            extracted_json=json.dumps({"title": f"job {i}"}), relevance_score=80.0,
        )
        store.update_state(conn, iid, "surfaced", from_state="extracted",
                           kind="deterministic", actor="system")
        ids.append(iid)

    events = {"ingest": 0, "gate": 0, "notified": None}

    async def fake_ingest(cfg, c):
        events["ingest"] += 1
        return []

    monkeypatch.setattr(run, "ingest", fake_ingest)
    monkeypatch.setattr(run.pipeline, "run_to_gate",
                        lambda *a, **k: events.__setitem__("gate", events["gate"] + 1))

    class FakeBot:
        async def notify_surfaced(self, item_id):
            pass

    fake_bot = FakeBot()
    cfg = _cfg(database_url=db_path)

    sent = asyncio.run(run.harvest(cfg, conn, fake_bot, object()))
    conn.close()

    assert events["ingest"] == 1
    # run_to_gate driven for each pending (extracted) item.
    assert events["gate"] >= len(extracted_ids)
    assert sorted(sent) == sorted(ids)  # both surfaced cards delivered


def test_run_main_one_shot_uses_shared_harvest(monkeypatch, pg_dsn):
    """run.main's one-shot path drives the SAME run.harvest callable that the
    scheduled job uses -> manual one-shot == scheduled harvest logic."""
    called = {"harvest": 0}

    async def fake_harvest(cfg, conn, bot, deps):
        called["harvest"] += 1
        return []

    monkeypatch.setattr(run, "harvest", fake_harvest)
    monkeypatch.setattr(run, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(run, "load_config", lambda: Config(database_url=pg_dsn))
    monkeypatch.setattr(run, "build_deps", lambda cfg: object())

    class FakeBot:
        def __init__(self, *a, **k):
            pass

        async def aclose(self):
            pass

    monkeypatch.setattr(run, "JobHunterBot", FakeBot)

    run.main()

    assert called["harvest"] == 1


# --- serve._amain: scheduler start + shutdown order in the finally ----------


class _FakeScheduler:
    def __init__(self):
        self.started = False
        self.shut = False
        self.added_jobs = []

    def add_job(self, func, *args, **kwargs):
        # serve._amain adds the staleness watchdog as a SECOND job via
        # serve.add_staleness_job(scheduler, ...). Record it so tests can assert
        # the wiring without a real APScheduler.
        self.added_jobs.append((func, kwargs.get("id")))

    def start(self):
        self.started = True

    def shutdown(self, wait=False):
        self.shut = True


class _ConnProxy:
    def __init__(self, real, on_close):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_on_close", on_close)

    def close(self):
        self._on_close()
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_serve_amain_starts_scheduler_and_shuts_it_down_before_session_and_conn(
    pg_dsn, monkeypatch
):
    """serve._amain starts the scheduler then polls; on teardown it shuts the
    scheduler down BEFORE bot.aclose() and conn.close()."""
    db_path = pg_dsn
    cfg = _cfg(database_url=db_path)

    events = []
    fake_sched = _FakeScheduler()

    # Capture ordering: scheduler.shutdown must precede aclose + conn.close.
    def fake_build_scheduler(factory, tz):
        # Record that the wired job factory targets the shared run.harvest path.
        events.append(("build_scheduler", factory))
        return fake_sched

    monkeypatch.setattr(serve, "build_scheduler", fake_build_scheduler)

    async def fake_run(self):
        # scheduler must be started before polling begins.
        assert fake_sched.started is True
        events.append("run")

    async def fake_aclose(self):
        # scheduler shut down before the session is closed.
        assert fake_sched.shut is True
        events.append("aclose")

    monkeypatch.setattr(bot_mod.JobHunterBot, "run", fake_run)
    monkeypatch.setattr(bot_mod.JobHunterBot, "aclose", fake_aclose)

    closed = {"conn": False}
    real_connect = store.connect

    def spy_connect(path):
        return _ConnProxy(real_connect(path),
                          lambda: closed.__setitem__("conn", True))

    monkeypatch.setattr(serve.store, "connect", spy_connect)

    asyncio.run(serve._amain(cfg))

    # Started, polled, then shut down + torn down in order.
    assert fake_sched.started is True
    assert fake_sched.shut is True
    assert events[0][0] == "build_scheduler"
    assert "run" in events and "aclose" in events
    assert events.index("run") < events.index("aclose")
    assert closed["conn"] is True


def test_serve_amain_shuts_scheduler_down_even_on_error(pg_dsn, monkeypatch):
    """If polling raises, the finally still shuts the scheduler down and closes
    the DB connection."""
    db_path = pg_dsn
    cfg = _cfg(database_url=db_path)

    fake_sched = _FakeScheduler()
    monkeypatch.setattr(serve, "build_scheduler", lambda f, tz: fake_sched)

    async def boom_run(self):
        raise KeyboardInterrupt

    async def fake_aclose(self):
        pass

    monkeypatch.setattr(bot_mod.JobHunterBot, "run", boom_run)
    monkeypatch.setattr(bot_mod.JobHunterBot, "aclose", fake_aclose)

    closed = {"conn": False}
    real_connect = store.connect
    monkeypatch.setattr(
        serve.store, "connect",
        lambda path: _ConnProxy(real_connect(path),
                                lambda: closed.__setitem__("conn", True)),
    )

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(serve._amain(cfg))

    assert fake_sched.shut is True
    assert closed["conn"] is True


def test_serve_amain_wires_real_run_harvest_as_the_job(pg_dsn, monkeypatch):
    """The job factory serve passes to build_scheduler, when called, invokes the
    SHARED run.harvest (same callable the one-shot uses) — not a fork."""
    db_path = pg_dsn
    cfg = _cfg(database_url=db_path)

    captured = {}

    def fake_build_scheduler(factory, tz):
        captured["factory"] = factory
        captured["tz"] = tz
        return _FakeScheduler()

    monkeypatch.setattr(serve, "build_scheduler", fake_build_scheduler)

    harvest_calls = {"n": 0}

    async def fake_harvest(cfg_, conn_, bot_, deps_):
        harvest_calls["n"] += 1
        return []

    monkeypatch.setattr(serve.run_mod, "harvest", fake_harvest)

    async def fake_run(self):
        pass

    async def fake_aclose(self):
        pass

    monkeypatch.setattr(bot_mod.JobHunterBot, "run", fake_run)
    monkeypatch.setattr(bot_mod.JobHunterBot, "aclose", fake_aclose)

    asyncio.run(serve._amain(cfg))

    # The factory exists and fires run.harvest when invoked (as APScheduler would).
    factory = captured["factory"]
    asyncio.run(factory())
    assert harvest_calls["n"] == 1


# ---------------------------------------------------------------------------
# Tester additions: explicit non-UTC tz assertion + function-identity check
# ---------------------------------------------------------------------------


def test_build_scheduler_belgrade_tz_is_not_utc():
    """EXPLICIT non-UTC guard: when SCHEDULE_TZ='Europe/Belgrade', the trigger
    timezone must NOT be UTC.  The existing test asserts == ZoneInfo("Belgrade")
    which is implicitly != UTC, but this test makes it explicit so a future
    refactor that accidentally hard-codes UTC is caught with a clear message.
    """
    from datetime import timezone as _timezone

    tz = serve.resolve_schedule_tz("Europe/Belgrade")
    scheduler = serve.build_scheduler(lambda: None, tz)
    trigger_tz = scheduler.get_jobs()[0].trigger.timezone

    # The scheduler must use the configured tz.
    assert trigger_tz == ZoneInfo("Europe/Belgrade"), (
        f"trigger tz must be Europe/Belgrade, got {trigger_tz}"
    )
    # And it must NOT be UTC (the core requirement: local tz, not hardcoded UTC).
    assert trigger_tz != _timezone.utc, (
        "trigger timezone must NOT be UTC when SCHEDULE_TZ='Europe/Belgrade'; "
        f"got {trigger_tz!r}"
    )


def test_serve_scheduled_job_and_run_main_use_same_function_object():
    """Function-identity: the job factory that serve wires to the scheduler and
    the one-shot path in run.main BOTH reference the SAME run.harvest function
    object — confirming no fork/duplication, just two call sites of one callable.

    This is the single test that proves identity without relying on call-count
    tracing via monkeypatching.
    """
    # serve imports run as run_mod; its job factory is:
    #   lambda: run_mod.harvest(cfg, conn, bot, deps)
    # The one-shot uses: await harvest(cfg, conn, bot, deps) where harvest == run.harvest.
    # Both resolve to the SAME module attribute.
    from job_hunter import run as run_module
    from job_hunter import serve as serve_module

    # serve_module.run_mod is the run module imported by serve.
    assert serve_module.run_mod is run_module, (
        "serve.run_mod must be the same module object as job_hunter.run"
    )
    # The harvest attribute on both references is the same callable.
    assert serve_module.run_mod.harvest is run_module.harvest, (
        "serve.run_mod.harvest must be the identical object as run.harvest "
        "(no forked copy)"
    )


# ---------------------------------------------------------------------------
# Daily-harvest regression: the scheduled job MUST be AWAITED on the loop
# ---------------------------------------------------------------------------


def test_serve_scheduled_job_is_coroutine_function_and_is_awaited(pg_dsn, monkeypatch):
    """The CORE regression. The job serve hands to the scheduler must be a
    coroutine function (so AsyncIOScheduler awaits it) AND, when invoked the way
    APScheduler invokes it (await job.func() on the loop), it must actually RUN
    harvest's body — proven by a side effect being recorded.

    The OLD code wired ``lambda: run.harvest(...)`` — a SYNC function that
    returns a coroutine. APScheduler would have called it, discarded the
    returned coroutine, never awaited it, and emitted "coroutine 'harvest' was
    never awaited"; the side effect below would NOT be recorded. We also assert
    no such RuntimeWarning is raised.
    """
    import warnings

    cfg = _cfg(database_url=pg_dsn)

    captured = {}

    def fake_build_scheduler(harvest_coro_func, tz):
        captured["func"] = harvest_coro_func
        captured["tz"] = tz
        return _FakeScheduler()

    monkeypatch.setattr(serve, "build_scheduler", fake_build_scheduler)

    ran = {"awaited": False}

    async def fake_harvest(cfg_, conn_, bot_, deps_):
        # Side effect proving the coroutine body actually executed (awaited).
        ran["awaited"] = True
        return []

    monkeypatch.setattr(serve.run_mod, "harvest", fake_harvest)

    async def fake_run(self):
        pass

    async def fake_aclose(self):
        pass

    monkeypatch.setattr(bot_mod.JobHunterBot, "run", fake_run)
    monkeypatch.setattr(bot_mod.JobHunterBot, "aclose", fake_aclose)

    asyncio.run(serve._amain(cfg))

    job_func = captured["func"]
    # 1) It is a coroutine function -> AsyncIOScheduler will AWAIT it.
    assert asyncio.iscoroutinefunction(job_func) is True, (
        "scheduled job must be an async def / coroutine function so the "
        "AsyncIOScheduler awaits it; a sync function returning a coroutine is "
        "the bug being fixed"
    )

    # 2) Invoke it the way APScheduler does (await on the loop) and prove the
    #    harvest body actually ran. A "never awaited" RuntimeWarning would be
    #    raised by the OLD sync-lambda wiring; assert none is raised.
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        asyncio.run(job_func())

    assert ran["awaited"] is True, (
        "the scheduled coroutine must be awaited and run harvest's body"
    )


def test_build_scheduler_rejects_nothing_but_proves_await_via_real_scheduler(pg_dsn):
    """End-to-end-ish: build the REAL scheduler with a coroutine-function job,
    start it on a running loop, fire the job immediately (run_job/modify
    next_run_time to now), and assert the coroutine's side effect happened.

    This proves the wiring through the actual AsyncIOScheduler — not just an
    iscoroutinefunction check — that the coroutine is awaited on the loop.
    """
    import warnings

    flag = {"ran": 0}

    async def harvest_func():
        flag["ran"] += 1

    async def driver():
        tz = serve.resolve_schedule_tz("Europe/Belgrade")
        scheduler = serve.build_scheduler(harvest_func, tz)
        scheduler.start()
        # Force the single job to run now on this loop.
        job = scheduler.get_jobs()[0]
        from datetime import datetime as _dt, timezone as _tz
        job.modify(next_run_time=_dt.now(_tz.utc))
        # Give the loop a few ticks to fire + await the coroutine.
        for _ in range(20):
            await asyncio.sleep(0.05)
            if flag["ran"]:
                break
        scheduler.shutdown(wait=False)

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        asyncio.run(driver())

    assert flag["ran"] == 1, (
        "the real AsyncIOScheduler must AWAIT the coroutine-function job on the "
        "running loop (the old sync-lambda-returning-coroutine never awaited it)"
    )


# ---------------------------------------------------------------------------
# "No new vacancies" notification (FIX 2)
# ---------------------------------------------------------------------------


def _seed_surfaced(conn, n):
    """Seed ``n`` SURFACED items; return their ids."""
    import json

    ids = []
    for i in range(n):
        iid = store.insert_item(
            conn, raw_text=f"job {i}", source_channel="@c", source_message_id=f"s{i}"
        )
        store.update_state(
            conn, iid, "extracted", from_state="discovered",
            kind="deterministic", actor="system",
            extracted_json=json.dumps({"title": f"job {i}"}), relevance_score=80.0,
        )
        store.update_state(conn, iid, "surfaced", from_state="extracted",
                           kind="deterministic", actor="system")
        ids.append(iid)
    return ids


def test_harvest_zero_new_sends_single_no_new_line(monkeypatch, pg_dsn):
    """ZERO-NEW path: ingest returns [] and nothing is surfaced -> exactly ONE
    one-line 'no new' message is sent to cfg.notify_chat_id."""
    conn = store.connect(pg_dsn)
    store.init_db(conn)

    async def fake_ingest(cfg, c):
        return []

    monkeypatch.setattr(run, "ingest", fake_ingest)
    monkeypatch.setattr(run.pipeline, "run_to_gate", lambda *a, **k: None)

    texts = []
    surfaced_calls = {"n": 0}

    class FakeBot:
        async def notify_text(self, text):
            texts.append(text)

        async def notify_surfaced(self, item_id):
            surfaced_calls["n"] += 1

    cfg = _cfg(database_url=pg_dsn)
    sent = asyncio.run(run.harvest(cfg, conn, FakeBot(), object()))
    conn.close()

    assert sent == []
    assert surfaced_calls["n"] == 0
    # Exactly one no-new line, single line, mentioning 0 new vacancies.
    assert len(texts) == 1
    assert "\n" not in texts[0]
    assert "0 new vacancies" in texts[0]
    assert "ingested 0" in texts[0]


def test_harvest_nonzero_does_not_send_no_new_line(monkeypatch, pg_dsn):
    """NON-zero path: with surfaced cards, the cards are sent and the 'no new'
    line is NOT sent."""
    conn = store.connect(pg_dsn)
    store.init_db(conn)
    ids = _seed_surfaced(conn, 2)

    async def fake_ingest(cfg, c):
        return []

    monkeypatch.setattr(run, "ingest", fake_ingest)
    monkeypatch.setattr(run.pipeline, "run_to_gate", lambda *a, **k: None)

    texts = []
    surfaced = []

    class FakeBot:
        async def notify_text(self, text):
            texts.append(text)

        async def notify_surfaced(self, item_id):
            surfaced.append(item_id)

    cfg = _cfg(database_url=pg_dsn)
    sent = asyncio.run(run.harvest(cfg, conn, FakeBot(), object()))
    conn.close()

    assert sorted(sent) == sorted(ids)
    assert sorted(surfaced) == sorted(ids)  # cards delivered
    assert texts == []  # NO no-new line when cards were sent


def test_notify_text_sends_to_notify_chat_id_one_line(monkeypatch):
    """JobHunterBot.notify_text sends the given text to cfg.notify_chat_id with
    the web preview disabled (Telegram send mocked)."""
    cfg = _cfg(database_url="postgresql://unused")
    bot = bot_mod.JobHunterBot(cfg, conn=object(), deps=object())

    sent = {}

    class FakeTgBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent["chat_id"] = chat_id
            sent["text"] = text
            sent["kwargs"] = kwargs

    # Bypass _ensure's real aiogram Bot construction.
    monkeypatch.setattr(bot, "_ensure", lambda: None)
    bot._bot = FakeTgBot()

    asyncio.run(bot.notify_text("🟢 Harvest done — 0 new vacancies (ingested 0)."))

    assert sent["chat_id"] == cfg.notify_chat_id
    assert sent["text"] == "🟢 Harvest done — 0 new vacancies (ingested 0)."
    assert sent["kwargs"].get("disable_web_page_preview") is True


# ---------------------------------------------------------------------------
# Adversarial regression: prove the NEW test catches the OLD bug
# These tests explicitly reconstruct the original broken wiring in test scope
# and assert it would have been caught. They do NOT modify production code.
# ---------------------------------------------------------------------------


def test_old_sync_lambda_wiring_fails_iscoroutinefunction_gate():
    """REGRESSION TEETH PROOF (part 1): the original broken wiring was
    ``lambda: run.harvest(cfg, conn, bot, deps)`` — a sync function that
    RETURNS a coroutine object without awaiting it.

    APScheduler routes jobs via ``iscoroutinefunction_partial``: a sync
    function takes the ``run_job`` path (synchronous executor in a thread),
    which calls the lambda, gets back a coroutine OBJECT, then DISCARDS it.
    The harvest body never executes and Python emits "coroutine was never
    awaited".

    This test reconstructs the old form in test scope and asserts:
    1) ``asyncio.iscoroutinefunction`` is False for the old lambda.
    2) ``asyncio.iscoroutinefunction`` is True for the new ``async def`` form.

    Because ``test_serve_scheduled_job_is_coroutine_function_and_is_awaited``
    asserts ``iscoroutinefunction(job_func) is True``, it would have FAILED
    against the old lambda — proving the test has real regression teeth.
    """
    async def fake_harvest(cfg_, conn_, bot_, deps_):
        return []

    # Old wiring: sync lambda returning a coroutine (the original bug).
    old_sync_lambda = lambda: fake_harvest(None, None, None, None)

    # New wiring: async def closure that awaits the coroutine.
    async def new_async_closure():
        await fake_harvest(None, None, None, None)

    assert asyncio.iscoroutinefunction(old_sync_lambda) is False, (
        "old sync lambda must NOT be a coroutine function — "
        "APScheduler discards its return value and harvest never runs"
    )
    assert asyncio.iscoroutinefunction(new_async_closure) is True, (
        "new async def closure must BE a coroutine function — "
        "APScheduler awaits it on the loop and harvest body runs"
    )


def test_old_sync_lambda_body_never_executes_via_real_apscheduler():
    """REGRESSION TEETH PROOF (part 2): fires the old sync-lambda through the
    REAL AsyncIOScheduler and proves the harvest body DOES NOT execute.

    APScheduler calls ``iscoroutinefunction_partial(job.func)`` to decide the
    executor path. A sync lambda -> ``run_job`` sync path -> calls the lambda,
    gets a coroutine object, stores it in ``retval``, then DISCARDS it when
    ``run_job`` returns. The coroutine body (flag increment) never runs.

    This is the exact failure mode of the original bug: harvest was silently
    a no-op on every scheduled fire. The ``assert flag["ran"] == 0`` below is
    the smoking gun; the new-wiring test (flag["ran"] == 1) in
    ``test_build_scheduler_rejects_nothing_but_proves_await_via_real_scheduler``
    is only possible because the fix changed to ``async def``.
    """
    import gc

    flag = {"ran": 0}

    async def harvest_body():
        flag["ran"] += 1

    # Old wiring: sync function returning the coroutine (never awaited).
    old_sync_lambda = lambda: harvest_body()

    async def driver():
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from datetime import datetime as _dt, timezone as _tz
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Europe/Belgrade")
        scheduler = AsyncIOScheduler(timezone=tz)
        trigger = CronTrigger(hour=10, minute=0, timezone=tz)
        scheduler.add_job(
            old_sync_lambda,
            trigger=trigger,
            id="old_harvest",
            coalesce=True,
            max_instances=1,
        )
        scheduler.start()
        job = scheduler.get_jobs()[0]
        job.modify(next_run_time=_dt.now(_tz.utc))
        for _ in range(20):
            await asyncio.sleep(0.05)
            gc.collect()
            if flag["ran"]:
                break
        scheduler.shutdown(wait=False)
        gc.collect()

    # Suppress the "never awaited" RuntimeWarning that the old wiring emits
    # (it goes through sys.unraisablehook / tp_finalize and may bypass the
    # warnings filter in CPython 3.9 — we capture it via a custom handler).
    caught_warnings = []

    def capture_warning(msg, category, filename, lineno, file=None, line=None):
        caught_warnings.append(str(msg))

    import warnings as _w
    old_showwarning = _w.showwarning
    _w.showwarning = capture_warning
    try:
        asyncio.run(driver())
    finally:
        _w.showwarning = old_showwarning

    # The harvest body must NOT have run: the old wiring discarded the coro.
    assert flag["ran"] == 0, (
        "OLD sync-lambda wiring: APScheduler must have discarded the returned "
        "coroutine — harvest body must NOT execute (flag stays 0). "
        f"Got flag['ran']={flag['ran']} — if this is 1, APScheduler's behaviour "
        "changed and the original bug may not be as described."
    )
    # A "never awaited" RuntimeWarning must have been emitted: proof that the
    # coroutine was created but abandoned.
    assert any("never awaited" in w for w in caught_warnings), (
        "old sync-lambda must have emitted 'coroutine was never awaited'; "
        f"got warnings: {caught_warnings}"
    )


def test_asyncio_run_on_sync_lambda_is_NOT_equivalent_to_apscheduler_execution():
    """REGRESSION TEETH GAP DOCUMENTATION: proves that the side-effect assertion
    ``assert ran['awaited'] is True`` in the core regression test provides NO
    regression teeth on its own.

    ``asyncio.run(job_func())`` for a SYNC job_func that returns a coroutine:
    - calls job_func() -> returns a coroutine object
    - passes that coroutine to asyncio.run() -> which AWAITS it
    - so the harvest body runs and the flag is set

    This is fundamentally different from how APScheduler fires a sync job:
    APScheduler calls job.func(*args) and DISCARDS the return value via
    ``run_job``; it never detects or awaits the returned coroutine.

    The ``asyncio.iscoroutinefunction`` assertion in the developer's test IS
    the load-bearing gate. This test documents that fact explicitly so future
    reviewers understand which assertions actually protect the invariant.
    """
    ran = {"awaited": False}

    async def fake_harvest(*_):
        ran["awaited"] = True
        return []

    # Old-style wiring: sync function returning a coroutine.
    old_sync_job_func = lambda: fake_harvest(None, None, None, None)

    # asyncio.run(old_sync_job_func()) DOES run the harvest body — this is
    # NOT how APScheduler fires the job. asyncio.run() detects the coroutine
    # returned by the call and awaits it. APScheduler does not.
    asyncio.run(old_sync_job_func())
    assert ran["awaited"] is True, (
        "asyncio.run on a sync-lambda returns-coroutine still awaits the coro; "
        "this side-effect check alone cannot distinguish old from new wiring"
    )

    # Reset
    ran["awaited"] = False

    # New-style wiring: async def closure.
    async def new_async_job_func():
        await fake_harvest(None, None, None, None)

    asyncio.run(new_async_job_func())
    assert ran["awaited"] is True

    # Key: iscoroutinefunction distinguishes them.
    assert asyncio.iscoroutinefunction(old_sync_job_func) is False
    assert asyncio.iscoroutinefunction(new_async_job_func) is True
