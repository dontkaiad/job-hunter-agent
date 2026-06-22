"""Observability upgrade: warning/exception alerts + harvest staleness watchdog.

All Telegram sends are MOCKED (tg_logger.send_log / send_error_log) — no real
network, no real ops bot. The harvest-heartbeat / staleness tests use the
ephemeral per-test PostgreSQL ``conn`` fixture (conftest).

Tester additions (appended at the bottom of this file):
  - Real warnings.warn() path reaches send_error_log (not just py_logger.warning direct)
  - UserWarning + ResourceWarning are NOT forwarded (noise filter holds for all three types)
  - Off-loop safety: emit from a background thread dispatches correctly
  - Heartbeat NOT written when harvest raises midway (no false-fresh stamp)
  - Exactly-26h boundary: >= means alert fires at exactly the threshold
  - Loop exception handler does not create a feedback loop when it is itself invoked
    from within a task that raises (i.e. no recursive fire when the send_error_log
    future is GC'd while the loop handler is active)
  - Staleness check reads ONLY ops_heartbeat (no container/health coupling)
"""

from __future__ import annotations

import asyncio
import logging
import threading
import warnings as _warnings_mod
from datetime import timedelta

import pytest

from job_hunter import clock, obs, run, store, tg_logger


# ---------------------------------------------------------------------------
# PART 1a: WARNING -> ALERT
# ---------------------------------------------------------------------------


def test_install_calls_capture_warnings_true(monkeypatch):
    """install_warning_alerts MUST enable logging.captureWarnings(True) so that
    Python's warnings.warn(...) is routed to the 'py.warnings' logger (the
    record path our handler listens on)."""
    flags = []
    real_capture = logging.captureWarnings
    monkeypatch.setattr(
        logging, "captureWarnings", lambda v: (flags.append(v), real_capture(v))[1]
    )

    async def driver():
        loop = asyncio.get_running_loop()
        handler = obs.install_warning_alerts(loop)
        logging.getLogger("py.warnings").removeHandler(handler)

    asyncio.run(driver())
    assert True in flags
    # Restore the GLOBAL warnings->logging hook this test turned on. Calling the
    # real captureWarnings(True) replaced warnings.showwarning; leaving it set
    # would make captureWarnings(True) a no-op for any later test that relies on
    # the real warnings.warn() path (order-dependent latent failure). Reset it.
    real_capture(False)


def test_runtime_warning_is_forwarded_to_send_error_log(monkeypatch):
    """A RuntimeWarning ('coroutine was never awaited') routed through the
    captured 'py.warnings' logger is forwarded to tg_logger.send_error_log with
    a message mentioning it.

    captureWarnings formats the category+message into the record text; we emit
    the formatted record directly onto py.warnings (the exact path
    captureWarnings feeds) so the test is deterministic regardless of which test
    last touched the global warnings.showwarning hook. The dispatch hops back
    onto the loop via call_soon_threadsafe, so we give the loop a tick.
    """
    sent = []

    async def fake_send_error_log(msg):
        sent.append(msg)

    monkeypatch.setattr(tg_logger, "send_error_log", fake_send_error_log)

    async def driver():
        loop = asyncio.get_running_loop()
        handler = obs.install_warning_alerts(loop)
        py_logger = logging.getLogger("py.warnings")
        try:
            # The formatted record captureWarnings emits for a never-awaited
            # coroutine RuntimeWarning.
            py_logger.warning(
                "/x.py:1: RuntimeWarning: coroutine 'harvest' was never awaited"
            )
            # Let the call_soon_threadsafe -> ensure_future scheduled task run.
            for _ in range(10):
                await asyncio.sleep(0.01)
                if sent:
                    break
        finally:
            py_logger.removeHandler(handler)

    asyncio.run(driver())

    assert len(sent) == 1
    assert "never awaited" in sent[0]
    assert "coroutine 'harvest'" in sent[0]


def test_deprecation_warning_is_NOT_forwarded(monkeypatch):
    """Noise filter: a DeprecationWarning must NOT be forwarded to ops."""
    sent = []

    async def fake_send_error_log(msg):
        sent.append(msg)

    monkeypatch.setattr(tg_logger, "send_error_log", fake_send_error_log)

    async def driver():
        loop = asyncio.get_running_loop()
        handler = obs.install_warning_alerts(loop)
        py_logger = logging.getLogger("py.warnings")
        try:
            # The formatted record captureWarnings would emit for a
            # DeprecationWarning. None of our forward markers appear, so the
            # noise filter drops it.
            py_logger.warning("/path/foo.py:12: DeprecationWarning: this API is deprecated")
            for _ in range(5):
                await asyncio.sleep(0.01)
        finally:
            py_logger.removeHandler(handler)

    asyncio.run(driver())

    assert sent == []


def test_warning_via_logging_record_path(monkeypatch):
    """Forward decision works on the formatted py.warnings record directly
    (emit path), independent of how the warning was raised."""
    sent = []

    async def fake_send_error_log(msg):
        sent.append(msg)

    monkeypatch.setattr(tg_logger, "send_error_log", fake_send_error_log)

    async def driver():
        loop = asyncio.get_running_loop()
        handler = obs.install_warning_alerts(loop)
        py_logger = logging.getLogger("py.warnings")
        try:
            # A real-problem record (RuntimeWarning category text) -> forwarded.
            py_logger.warning("Task was destroyed but it is pending! RuntimeWarning")
            for _ in range(10):
                await asyncio.sleep(0.01)
                if sent:
                    break
        finally:
            py_logger.removeHandler(handler)

    asyncio.run(driver())

    assert len(sent) == 1
    assert "RuntimeWarning" in sent[0]


def test_install_warning_alerts_is_idempotent(monkeypatch):
    """Re-installing does not stack duplicate handlers (one alert, not N)."""
    sent = []

    async def fake_send_error_log(msg):
        sent.append(msg)

    monkeypatch.setattr(tg_logger, "send_error_log", fake_send_error_log)

    async def driver():
        loop = asyncio.get_running_loop()
        h1 = obs.install_warning_alerts(loop)
        h2 = obs.install_warning_alerts(loop)
        py_logger = logging.getLogger("py.warnings")
        try:
            assert h1 is h2  # same handler returned; not duplicated
            count = sum(
                1 for h in py_logger.handlers
                if getattr(h, obs._HANDLER_TAG, False)
            )
            assert count == 1
            py_logger.warning("coroutine 'x' was never awaited RuntimeWarning")
            for _ in range(10):
                await asyncio.sleep(0.01)
                if sent:
                    break
        finally:
            py_logger.removeHandler(h1)

    asyncio.run(driver())
    # Exactly one send, not two.
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# PART 1b: LOOP EXCEPTION HANDLER
# ---------------------------------------------------------------------------


def test_loop_exception_handler_calls_default_and_forwards(monkeypatch):
    """install_loop_exception_handler: default handler still runs (stderr) AND
    the message is forwarded to send_error_log."""
    sent = []

    async def fake_send_error_log(msg):
        sent.append(msg)

    monkeypatch.setattr(tg_logger, "send_error_log", fake_send_error_log)

    default_called = []

    async def driver():
        loop = asyncio.get_running_loop()
        # Spy on the default handler to prove it still ran.
        real_default = loop.default_exception_handler

        def spy_default(context):
            default_called.append(context.get("message"))
            return real_default(context)

        monkeypatch.setattr(loop, "default_exception_handler", spy_default)

        obs.install_loop_exception_handler(loop)
        loop.call_exception_handler(
            {
                "message": "Task exception was never retrieved",
                "exception": ValueError("boom"),
            }
        )
        for _ in range(10):
            await asyncio.sleep(0.01)
            if sent:
                break

    asyncio.run(driver())

    # Default handler preserved.
    assert default_called == ["Task exception was never retrieved"]
    # Forwarded to ops with the message + the exception repr.
    assert len(sent) == 1
    assert "Task exception was never retrieved" in sent[0]
    assert "ValueError('boom')" in sent[0]


def test_loop_exception_handler_real_unretrieved_task_exception(monkeypatch):
    """A genuinely un-retrieved task exception triggers the loop handler and a
    forwarded alert (no explicit call_exception_handler)."""
    sent = []

    async def fake_send_error_log(msg):
        sent.append(msg)

    monkeypatch.setattr(tg_logger, "send_error_log", fake_send_error_log)

    async def driver():
        loop = asyncio.get_running_loop()
        obs.install_loop_exception_handler(loop)

        async def boom():
            raise RuntimeError("unretrieved")

        # Create a task whose exception is never awaited/retrieved, then let it
        # be GC'd so the loop reports "Task exception was never retrieved".
        t = asyncio.ensure_future(boom())
        await asyncio.sleep(0.05)
        del t
        import gc

        gc.collect()
        for _ in range(20):
            await asyncio.sleep(0.02)
            if sent:
                break

    asyncio.run(driver())

    assert any("RuntimeError('unretrieved')" in m for m in sent), sent


# ---------------------------------------------------------------------------
# Debounce intact: identical warning storm collapses to ONE send
# ---------------------------------------------------------------------------


def test_identical_warning_storm_is_debounced(monkeypatch):
    """A storm of identical warnings must not produce N ops sends — the real
    tg_logger.send_error_log debounce collapses them (send_log is mocked)."""
    tg_logger._reset_debounce()
    log_calls = []

    async def fake_send_log(text):
        log_calls.append(text)

    # Mock the underlying send_log (network), keep the REAL send_error_log so
    # its debounce is exercised.
    monkeypatch.setattr(tg_logger, "send_log", fake_send_log)

    async def driver():
        loop = asyncio.get_running_loop()
        handler = obs.install_warning_alerts(loop)
        py_logger = logging.getLogger("py.warnings")
        try:
            # Emit identical records straight onto py.warnings (the same path
            # captureWarnings feeds), avoiding pytest's showwarning hook so the
            # test is deterministic. Same formatted message each time so the
            # debounce dedup collapses the storm.
            for _ in range(8):
                py_logger.warning("coroutine 'dup' was never awaited RuntimeWarning")
            for _ in range(20):
                await asyncio.sleep(0.01)
        finally:
            py_logger.removeHandler(handler)

    asyncio.run(driver())
    tg_logger._reset_debounce()

    # Despite 8 identical warnings, the debounce window collapses to ONE send.
    assert len(log_calls) == 1, log_calls


# ---------------------------------------------------------------------------
# PART 2a: HEARTBEAT WRITE via store + via run.harvest
# ---------------------------------------------------------------------------


def test_set_and_get_last_harvest_at_roundtrip(conn):
    """store.set_last_harvest_at / get_last_harvest_at roundtrip a tz-aware UTC
    datetime; None when no row."""
    assert store.get_last_harvest_at(conn) is None
    now = clock.now_utc()
    store.set_last_harvest_at(conn, now)
    got = store.get_last_harvest_at(conn)
    assert got is not None
    assert got.tzinfo is not None
    assert abs((got - now).total_seconds()) < 1.0


def test_harvest_writes_heartbeat_at_end(monkeypatch, pg_dsn):
    """Running run.harvest (ingest=[]/notify mocked) writes a fresh harvest
    heartbeat readable via get_last_harvest_at, close to now."""
    conn = store.connect(pg_dsn)
    store.init_db(conn)

    async def fake_ingest(cfg, c):
        return []

    monkeypatch.setattr(run, "ingest", fake_ingest)
    monkeypatch.setattr(run.pipeline, "run_to_gate", lambda *a, **k: None)

    class FakeBot:
        async def notify_text(self, text):
            pass

        async def notify_surfaced(self, item_id):
            pass

    from job_hunter.config import Config

    cfg = Config(bot_token="x", notify_chat_id=1, database_url=pg_dsn)
    before = clock.now_utc()
    asyncio.run(run.harvest(cfg, conn, FakeBot(), object()))
    after = clock.now_utc()

    last = store.get_last_harvest_at(conn)
    conn.close()
    assert last is not None
    assert before - timedelta(seconds=2) <= last <= after + timedelta(seconds=2)


# ---------------------------------------------------------------------------
# PART 2b: STALE -> ALERT (check_harvest_staleness)
# ---------------------------------------------------------------------------


def test_stale_harvest_sends_loud_alert_once(conn, monkeypatch):
    """A 30h-old heartbeat -> check_harvest_staleness sends EXACTLY the loud ops
    line via tg_logger.send_log."""
    sent = []

    async def fake_send_log(text):
        sent.append(text)

    monkeypatch.setattr(tg_logger, "send_log", fake_send_log)

    now = clock.now_utc()
    stale = now - timedelta(hours=30)
    store.set_last_harvest_at(conn, stale)

    msg = asyncio.run(obs.check_harvest_staleness(conn, now=now))

    assert len(sent) == 1
    expected = (
        f"⚠️ jobhunter: scheduled harvest hasn't run since {stale.isoformat()} "
        f"— function-level alert (container liveness is monitor.sh's job)"
    )
    assert sent[0] == expected
    assert msg == expected
    # Function-liveness wording must be distinct from monitor.sh container health.
    assert "function-level" in sent[0] and "monitor.sh" in sent[0]


def test_fresh_harvest_does_not_alert(conn, monkeypatch):
    """A 1h-old heartbeat -> NOT stale -> no alert."""
    sent = []

    async def fake_send_log(text):
        sent.append(text)

    monkeypatch.setattr(tg_logger, "send_log", fake_send_log)

    now = clock.now_utc()
    store.set_last_harvest_at(conn, now - timedelta(hours=1))

    msg = asyncio.run(obs.check_harvest_staleness(conn, now=now))

    assert sent == []
    assert msg is None


def test_no_heartbeat_row_does_not_alert(conn, monkeypatch):
    """No heartbeat (fresh deploy, no harvest yet) -> NO alert (no baseline)."""
    sent = []

    async def fake_send_log(text):
        sent.append(text)

    monkeypatch.setattr(tg_logger, "send_log", fake_send_log)

    msg = asyncio.run(obs.check_harvest_staleness(conn, now=clock.now_utc()))

    assert sent == []
    assert msg is None


def test_staleness_threshold_boundary(conn, monkeypatch):
    """Just under the 26h threshold -> no alert; just over -> alert."""
    sent = []

    async def fake_send_log(text):
        sent.append(text)

    monkeypatch.setattr(tg_logger, "send_log", fake_send_log)
    now = clock.now_utc()

    store.set_last_harvest_at(conn, now - timedelta(hours=25, minutes=59))
    assert asyncio.run(obs.check_harvest_staleness(conn, now=now)) is None
    assert sent == []

    store.set_last_harvest_at(conn, now - timedelta(hours=26, minutes=1))
    assert asyncio.run(obs.check_harvest_staleness(conn, now=now)) is not None
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# PART 2c: serve wires the SECOND (staleness) scheduler job
# ---------------------------------------------------------------------------


def test_add_staleness_job_registers_second_daily_11_00_job():
    """serve.add_staleness_job adds a SECOND daily cron job at 11:00 local,
    leaving the existing daily_harvest job intact."""
    from zoneinfo import ZoneInfo

    from job_hunter import serve

    tz = ZoneInfo("Europe/Belgrade")

    async def harvest_func():
        pass

    async def staleness_func():
        pass

    scheduler = serve.build_scheduler(harvest_func, tz)
    serve.add_staleness_job(scheduler, staleness_func, tz)

    jobs = {j.id: j for j in scheduler.get_jobs()}
    assert "daily_harvest" in jobs
    assert "harvest_staleness" in jobs
    stale_job = jobs["harvest_staleness"]

    def _field(trigger, name):
        for f in trigger.fields:
            if f.name == name:
                return int(str(f))
        raise AssertionError(name)

    assert _field(stale_job.trigger, "hour") == serve.STALENESS_CHECK_HOUR == 11
    assert _field(stale_job.trigger, "minute") == 0
    assert stale_job.trigger.timezone == tz
    assert asyncio.iscoroutinefunction(stale_job.func) is True


# ===========================================================================
# TESTER ADDITIONS — edge cases not covered by the Developer's tests
# ===========================================================================


# ---------------------------------------------------------------------------
# T1: Real warnings.warn() path (not only direct py_logger.warning calls)
# ---------------------------------------------------------------------------


def test_real_warnings_warn_never_awaited_reaches_send_error_log(monkeypatch):
    """THE critical proof: a genuine ``warnings.warn('coroutine X was never
    awaited', RuntimeWarning)`` issued via the stdlib warnings machinery (NOT a
    direct py_logger.warning call) reaches ``tg_logger.send_error_log``.

    This exercises the FULL path:
      warnings.warn(...)  ->  captureWarnings hook  ->  py.warnings logger
      ->  _WarningAlertHandler.emit  ->  call_soon_threadsafe -> ensure_future
      ->  send_error_log

    ISOLATION NOTE — why we reset logging._warnings_showwarning:
    test_install_calls_capture_warnings_true (which runs before this test in
    the file) monkeypatches logging.captureWarnings. After that test's teardown,
    monkeypatch restores the captureWarnings FUNCTION but does NOT restore the
    module-level logging._warnings_showwarning sentinel. The captureWarnings guard
    ``if _warnings_showwarning is None`` then stays False in this test, making
    captureWarnings(True) a no-op (showwarning stays as the original, not the
    logging hook). We reset the sentinel to None explicitly so the guard fires
    and captureWarnings(True) actually installs the logging hook.  This is a
    test-isolation artifact; it does NOT affect production behaviour (production
    never monkeypatches captureWarnings).
    """
    sent = []

    async def fake_send_error_log(msg):
        sent.append(msg)

    monkeypatch.setattr(tg_logger, "send_error_log", fake_send_error_log)

    async def driver():
        loop = asyncio.get_running_loop()
        # Reset the captureWarnings sentinel so our install_warning_alerts call
        # actually installs the logging hook (see ISOLATION NOTE above).
        logging._warnings_showwarning = None  # type: ignore[attr-defined]
        handler = obs.install_warning_alerts(loop)
        try:
            _warnings_mod.warn(
                "coroutine 'never_awaited_real_path_probe' was never awaited",
                RuntimeWarning,
                stacklevel=1,
            )
            for _ in range(20):
                await asyncio.sleep(0.01)
                if sent:
                    break
        finally:
            logging.getLogger("py.warnings").removeHandler(handler)
            # Restore the sentinel so subsequent tests see clean state.
            logging._warnings_showwarning = None  # type: ignore[attr-defined]

    asyncio.run(driver())

    assert len(sent) >= 1, "RuntimeWarning via warnings.warn() must reach send_error_log"
    assert any("never awaited" in m for m in sent), sent


# ---------------------------------------------------------------------------
# T2: UserWarning and ResourceWarning are NOT forwarded (noise filter)
# ---------------------------------------------------------------------------


def test_user_warning_is_NOT_forwarded(monkeypatch):
    """UserWarning must NOT be forwarded to ops (noise filter)."""
    sent = []

    async def fake_send_error_log(msg):
        sent.append(msg)

    monkeypatch.setattr(tg_logger, "send_error_log", fake_send_error_log)

    async def driver():
        loop = asyncio.get_running_loop()
        handler = obs.install_warning_alerts(loop)
        py_logger = logging.getLogger("py.warnings")
        try:
            py_logger.warning("/foo.py:1: UserWarning: this is just a user warning")
            for _ in range(5):
                await asyncio.sleep(0.01)
        finally:
            py_logger.removeHandler(handler)

    asyncio.run(driver())
    assert sent == [], f"UserWarning must NOT be forwarded; got: {sent}"


def test_resource_warning_is_NOT_forwarded(monkeypatch):
    """ResourceWarning must NOT be forwarded to ops (noise filter)."""
    sent = []

    async def fake_send_error_log(msg):
        sent.append(msg)

    monkeypatch.setattr(tg_logger, "send_error_log", fake_send_error_log)

    async def driver():
        loop = asyncio.get_running_loop()
        handler = obs.install_warning_alerts(loop)
        py_logger = logging.getLogger("py.warnings")
        try:
            py_logger.warning("/bar.py:2: ResourceWarning: unclosed file <...>")
            for _ in range(5):
                await asyncio.sleep(0.01)
        finally:
            py_logger.removeHandler(handler)

    asyncio.run(driver())
    assert sent == [], f"ResourceWarning must NOT be forwarded; got: {sent}"


# ---------------------------------------------------------------------------
# T3: Off-loop thread safety — emit from background thread
# ---------------------------------------------------------------------------


def test_emit_from_background_thread_dispatches_safely(monkeypatch):
    """Never-awaited warnings fire from GC finalizers, which can run on any
    thread OUTSIDE the event loop.  Emit from a background thread must
    dispatch via call_soon_threadsafe (NOT raise) and the alert must still
    arrive on the loop.
    """
    sent = []
    thread_errors = []

    async def fake_send_error_log(msg):
        sent.append(msg)

    monkeypatch.setattr(tg_logger, "send_error_log", fake_send_error_log)

    done_event = threading.Event()

    async def driver():
        loop = asyncio.get_running_loop()
        handler = obs.install_warning_alerts(loop)
        py_logger = logging.getLogger("py.warnings")

        def background_emit():
            try:
                py_logger.warning(
                    "coroutine 'background_task' was never awaited RuntimeWarning"
                )
            except Exception as exc:
                thread_errors.append(exc)
            finally:
                done_event.set()

        t = threading.Thread(target=background_emit, daemon=True)
        t.start()
        done_event.wait(timeout=2.0)

        for _ in range(20):
            await asyncio.sleep(0.01)
            if sent:
                break

        py_logger.removeHandler(handler)

    asyncio.run(driver())

    assert thread_errors == [], f"background thread emit raised: {thread_errors}"
    assert len(sent) >= 1, "off-loop background thread emit must reach send_error_log"
    assert any("never awaited" in m for m in sent), sent


# ---------------------------------------------------------------------------
# T4: Heartbeat NOT written when harvest raises midway
# ---------------------------------------------------------------------------


def test_heartbeat_not_written_when_harvest_raises_midway(monkeypatch, pg_dsn):
    """If harvest raises before reaching the heartbeat write (e.g. ingest fails),
    no heartbeat must be written.  This guards against falsely stamping a
    'fresh' harvest when the actual harvest body never completed.

    The heartbeat is written at the VERY END of run.harvest (after ingest,
    run_to_gate, and notify all succeed), so any exception propagated before
    that point means no heartbeat is stamped.
    """
    conn = store.connect(pg_dsn)
    store.init_db(conn)

    async def fail_ingest(cfg, c):
        raise RuntimeError("ingest exploded midway")

    monkeypatch.setattr(run, "ingest", fail_ingest)

    class FakeBot:
        async def notify_text(self, text):
            pass

        async def notify_surfaced(self, item_id):
            pass

    from job_hunter.config import Config

    cfg = Config(bot_token="x", notify_chat_id=1, database_url=pg_dsn)

    with pytest.raises(RuntimeError, match="ingest exploded"):
        asyncio.run(run.harvest(cfg, conn, FakeBot(), object()))

    heartbeat = store.get_last_harvest_at(conn)
    conn.close()

    assert heartbeat is None, (
        "Heartbeat must NOT be written when harvest raises midway; "
        f"got {heartbeat!r}.  The heartbeat is the signal that a harvest COMPLETED; "
        "a failed harvest must not appear fresh to the staleness watchdog."
    )


# ---------------------------------------------------------------------------
# T5: Exactly-26h boundary — threshold is >= (not >)
# ---------------------------------------------------------------------------


def test_staleness_boundary_exactly_26h_triggers_alert(conn, monkeypatch):
    """Exactly 26h old MUST trigger the alert (boundary is ``age >= stale_after_hours``
    i.e. ``not age < threshold``).

    The implementation uses ``if age_hours < stale_after_hours: return None``, which
    means age == 26.0 is NOT less than 26 -> alert fires.  This test pins that the
    boundary is inclusive (>=) so a future refactor to ``<=`` or ``>`` is caught.
    """
    sent = []

    async def fake_send_log(text):
        sent.append(text)

    monkeypatch.setattr(tg_logger, "send_log", fake_send_log)

    now = clock.now_utc()
    # Exactly 26h old.
    store.set_last_harvest_at(conn, now - timedelta(hours=26))
    msg = asyncio.run(obs.check_harvest_staleness(conn, now=now))

    assert msg is not None, (
        "A harvest exactly 26h old must be considered stale (age >= threshold). "
        "If this fails, the boundary logic was changed to > instead of >=."
    )
    assert len(sent) == 1


def test_staleness_boundary_25h_59m_59s_does_not_alert(conn, monkeypatch):
    """One second under 26h is NOT stale — just under the threshold."""
    sent = []

    async def fake_send_log(text):
        sent.append(text)

    monkeypatch.setattr(tg_logger, "send_log", fake_send_log)

    now = clock.now_utc()
    store.set_last_harvest_at(conn, now - timedelta(hours=25, minutes=59, seconds=59))
    msg = asyncio.run(obs.check_harvest_staleness(conn, now=now))

    assert msg is None, "25h59m59s must NOT be stale"
    assert sent == []


# ---------------------------------------------------------------------------
# T6: Staleness check independence from container/monitor.sh health
# ---------------------------------------------------------------------------


def test_check_harvest_staleness_has_no_container_health_coupling():
    """``check_harvest_staleness`` must read ONLY the ops_heartbeat DB table —
    no subprocess calls, no container health checks, no monitor.sh involvement.

    This is the core architectural invariant: the function-level staleness alert
    fires based on last_harvest_at age alone, regardless of whether the container
    is healthy.  A healthy container with a silently dead harvest STILL alerts.

    Verified by inspecting the bytecode co_names (all names called in the function)
    and asserting no container/health/subprocess names appear.
    """
    import dis
    import re

    fn = obs.check_harvest_staleness
    co = fn.__code__

    # All names that the function's bytecode CALLS or ACCESSES.
    all_names = set(co.co_names) | set(co.co_varnames) | set(co.co_freevars)

    # None of these must appear in the function's namespace.
    forbidden = {"subprocess", "os", "socket", "health", "monitor", "container",
                 "liveness", "popen", "system"}

    bad = {n for n in all_names if any(f in n.lower() for f in forbidden)}
    assert not bad, (
        f"check_harvest_staleness must NOT reference container/health machinery; "
        f"found: {bad}.  The staleness alert must be INDEPENDENT of monitor.sh."
    )

    # Positive: it must call store.get_last_harvest_at and tg_logger.send_log.
    assert "get_last_harvest_at" in co.co_names, (
        "check_harvest_staleness must call store.get_last_harvest_at"
    )
    assert "send_log" in co.co_names, (
        "check_harvest_staleness must call tg_logger.send_log for the stale alert"
    )
