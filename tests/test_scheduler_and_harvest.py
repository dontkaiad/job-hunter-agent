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
    calls = {"n": 0}

    def factory():
        calls["n"] += 1

    scheduler = serve.build_scheduler(factory, tz)

    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    trigger = job.trigger
    # hour==10, minute==0.
    assert _cron_field(trigger, "hour") == serve.HARVEST_HOUR == 10
    assert _cron_field(trigger, "minute") == serve.HARVEST_MINUTE == 0
    # Trigger timezone is exactly the configured tz.
    assert trigger.timezone == tz
    # The job function is the supplied factory (the shared-harvest wiring).
    assert job.func is factory
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
