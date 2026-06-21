"""Deploy-hardening pass: DB persistence, ops-logging, heartbeat healthcheck.

All network / Telegram / LLM / polling is MOCKED — no real calls. Covers:

  Part A — DB path: serve._amain / store.connect open the DB at exactly the
    absolute DB_PATH (a nested tmp path), creating the file there (not relative).

  Part B — tg_logger:
    * no-ops cleanly (no HTTP) when TG_LOG_BOT_TOKEN / TG_LOG_CHAT_ID unset;
    * sends with chat_id + thread_id + text when configured (httpx mocked);
    * swallows a network error (httpx raises) without propagating;
    * startup ping (@dp.startup hook / JobHunterBot.on_startup) calls send_log
      once with the ✅ text containing GIT_SHA;
    * error handler (@dp.errors hook / on_error) debounces: same error twice
      rapidly -> send_log once; after the window -> sent again.

  Part C — heartbeat:
    * one tick writes an int epoch string to the given path;
    * the write body swallows an error (no raise).
"""

from __future__ import annotations

import asyncio
import os

import pytest

from job_hunter import bot, serve, store, tg_logger
from job_hunter.config import Config, load_config
from job_hunter.pipeline import Deps


VALID_TOKEN = "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
ALLOWED_UID = 777


# ---------------------------------------------------------------------------
# Part A — DB connection: DATABASE_URL flows through to the open connection
# ---------------------------------------------------------------------------


def test_config_reads_database_url_from_env(monkeypatch):
    """DATABASE_URL from the environment flows verbatim into cfg.database_url."""
    dsn = "postgresql://user:pass@db-host:5432/jobhunter"
    monkeypatch.setenv("DATABASE_URL", dsn)
    cfg = load_config()
    assert cfg.database_url == dsn


def test_config_database_url_default_is_empty_when_unset(monkeypatch):
    """With DATABASE_URL UNSET the default is empty; cfg.require('database_url')
    then fails fast at startup rather than silently using a bogus DSN."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = load_config()
    assert cfg.database_url == ""
    with pytest.raises(RuntimeError):
        cfg.require("database_url")


def test_serve_opens_db_at_exact_database_url(pg_dsn, tmp_path, monkeypatch):
    """serve._amain opens the DB at EXACTLY cfg.database_url and runs init_db
    against it (schema present)."""
    db_path = pg_dsn

    cfg = Config(
        bot_token=VALID_TOKEN,
        notify_chat_id=123,
        allowed_user_ids={ALLOWED_UID},
        database_url=db_path,
    )

    opened_paths = []
    real_connect = store.connect

    def spy_connect(path):
        opened_paths.append(path)
        return real_connect(path)

    monkeypatch.setattr(serve.store, "connect", spy_connect)

    async def fake_run(self):
        # Prove init_db ran against the connection opened at the exact DSN.
        row = self.conn.execute(
            "SELECT to_regclass('public.work_items') AS t"
        ).fetchone()
        assert row is not None and row["t"] is not None

    async def fake_aclose(self):
        pass

    monkeypatch.setattr(bot.JobHunterBot, "run", fake_run)
    monkeypatch.setattr(bot.JobHunterBot, "aclose", fake_aclose)
    # Point the heartbeat at tmp so serve doesn't touch /tmp/heartbeat.
    monkeypatch.setenv("HEARTBEAT_PATH", str(tmp_path / "hb"))

    asyncio.run(serve._amain(cfg))

    assert opened_paths == [db_path]


# ---------------------------------------------------------------------------
# Part B — tg_logger
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, raise_for_status_exc=None):
        self._exc = raise_for_status_exc

    def raise_for_status(self):
        if self._exc is not None:
            raise self._exc


class _FakeAsyncClient:
    """Records the single POST; mimics httpx.AsyncClient as an async ctx mgr."""

    last_instance = None

    def __init__(self, *args, **kwargs):
        self.posts = []
        self.post_exc = None
        _FakeAsyncClient.last_instance = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self.posts.append({"url": url, "json": json})
        if self.post_exc is not None:
            raise self.post_exc
        return _FakeResponse()


@pytest.fixture(autouse=True)
def _reset_tg_logger_state(monkeypatch):
    """Reset the module-level one-time-warning + debounce state per test."""
    monkeypatch.setattr(tg_logger, "_warned_unconfigured", False, raising=False)
    tg_logger._reset_debounce()
    yield
    tg_logger._reset_debounce()


def test_tg_logger_noop_when_unconfigured(monkeypatch):
    """No token / chat id -> NO HTTP call, returns None, never raises."""
    monkeypatch.delenv("TG_LOG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_LOG_CHAT_ID", raising=False)
    monkeypatch.delenv("TG_LOG_THREAD_JOBHUNTER", raising=False)

    called = {"n": 0}

    def boom_client(*a, **k):
        called["n"] += 1
        raise AssertionError("httpx must NOT be invoked when unconfigured")

    monkeypatch.setattr(tg_logger.httpx, "AsyncClient", boom_client)

    result = asyncio.run(tg_logger.send_log("hello"))
    assert result is None
    assert called["n"] == 0


def test_tg_logger_noop_when_only_token_set(monkeypatch):
    """Token set but chat id missing -> still a no-op (both are required)."""
    monkeypatch.setenv("TG_LOG_BOT_TOKEN", "t")
    monkeypatch.delenv("TG_LOG_CHAT_ID", raising=False)

    def boom_client(*a, **k):
        raise AssertionError("must not send without chat id")

    monkeypatch.setattr(tg_logger.httpx, "AsyncClient", boom_client)
    assert asyncio.run(tg_logger.send_log("x")) is None


def test_tg_logger_sends_with_chat_thread_and_text(monkeypatch):
    """Configured -> posts to sendMessage with chat_id + thread + text."""
    monkeypatch.setenv("TG_LOG_BOT_TOKEN", "BOTTOKEN")
    monkeypatch.setenv("TG_LOG_CHAT_ID", "-100999")
    monkeypatch.setenv("TG_LOG_THREAD_JOBHUNTER", "30")

    monkeypatch.setattr(tg_logger.httpx, "AsyncClient", _FakeAsyncClient)

    asyncio.run(tg_logger.send_log("✅ jobhunter поднялся abc123"))

    inst = _FakeAsyncClient.last_instance
    assert inst is not None and len(inst.posts) == 1
    post = inst.posts[0]
    assert "/botBOTTOKEN/sendMessage" in post["url"]
    body = post["json"]
    assert body["chat_id"] == "-100999"
    assert body["message_thread_id"] == "30"
    assert body["text"] == "✅ jobhunter поднялся abc123"
    assert body["disable_web_page_preview"] is True


def test_tg_logger_omits_thread_when_unset(monkeypatch):
    """No thread id -> message_thread_id is omitted from the payload."""
    monkeypatch.setenv("TG_LOG_BOT_TOKEN", "BOTTOKEN")
    monkeypatch.setenv("TG_LOG_CHAT_ID", "-100999")
    monkeypatch.delenv("TG_LOG_THREAD_JOBHUNTER", raising=False)

    monkeypatch.setattr(tg_logger.httpx, "AsyncClient", _FakeAsyncClient)
    asyncio.run(tg_logger.send_log("hi"))

    body = _FakeAsyncClient.last_instance.posts[0]["json"]
    assert "message_thread_id" not in body


def test_tg_logger_swallows_network_error(monkeypatch):
    """A network/Telegram failure is caught — no exception propagates."""
    monkeypatch.setenv("TG_LOG_BOT_TOKEN", "BOTTOKEN")
    monkeypatch.setenv("TG_LOG_CHAT_ID", "-100999")

    class _RaisingClient(_FakeAsyncClient):
        async def post(self, url, json=None):
            raise RuntimeError("network down")

    monkeypatch.setattr(tg_logger.httpx, "AsyncClient", _RaisingClient)

    # Must NOT raise.
    assert asyncio.run(tg_logger.send_log("x")) is None


def test_tg_logger_swallows_http_status_error(monkeypatch):
    """A non-2xx (raise_for_status) is caught too."""
    monkeypatch.setenv("TG_LOG_BOT_TOKEN", "BOTTOKEN")
    monkeypatch.setenv("TG_LOG_CHAT_ID", "-100999")

    class _BadStatusClient(_FakeAsyncClient):
        async def post(self, url, json=None):
            return _FakeResponse(raise_for_status_exc=RuntimeError("400"))

    monkeypatch.setattr(tg_logger.httpx, "AsyncClient", _BadStatusClient)
    assert asyncio.run(tg_logger.send_log("x")) is None


# --- startup ping -----------------------------------------------------------


def _make_bot():
    cfg = Config(
        bot_token=VALID_TOKEN, notify_chat_id=1, allowed_user_ids={1},
    )
    # These tests exercise on_startup / on_error which never touch the DB; the
    # bot just needs SOME connection object. Connect to the ALWAYS-PRESENT base
    # maintenance DB on the test server (NOT the per-test clone, which only
    # exists inside a test that requested the postgresql fixture).
    import os as _os

    host = _os.environ.get("PGHOST", "127.0.0.1")
    port = _os.environ.get("PGPORT", "55432")
    user = _os.environ.get("PGUSER", "jobhunter")
    password = _os.environ.get("PGPASSWORD", "jobhunter")
    maint_db = _os.environ.get("PGMAINTDB", "jobhunter_test")
    dsn = f"postgresql://{user}:{password}@{host}:{port}/{maint_db}"
    b = bot.JobHunterBot(cfg, store.connect(dsn), Deps(llm_client=None, fx=None))
    return b


def test_startup_ping_calls_send_log_with_git_sha(monkeypatch):
    """on_startup (the @dp.startup hook body) calls send_log once with the ✅
    text containing GIT_SHA."""
    monkeypatch.setenv("GIT_SHA", "deadbee")
    sent = []

    async def fake_send_log(text):
        sent.append(text)

    monkeypatch.setattr(tg_logger, "send_log", fake_send_log)

    b = _make_bot()
    asyncio.run(b.on_startup())

    assert len(sent) == 1
    assert sent[0] == "✅ jobhunter поднялся deadbee"


def test_startup_hook_registered_on_existing_dispatcher():
    """The startup + errors hooks are wired onto the SINGLE existing dispatcher
    (not a second Dispatcher)."""
    b = _make_bot()

    async def go():
        # _ensure builds the aiogram Dispatcher, which needs a running loop.
        b._ensure()
        return b._dp

    dp = asyncio.run(go())
    assert len(dp.startup.handlers) == 1
    assert len(dp.errors.handlers) == 1


# --- error handler + debounce ----------------------------------------------


def test_error_handler_fires_and_debounces(monkeypatch):
    """on_error reports to ops; identical errors twice rapidly -> ONE send;
    after the debounce window -> sent again."""
    sent = []

    async def fake_send_log(text):
        sent.append(text)

    monkeypatch.setattr(tg_logger, "send_log", fake_send_log)
    tg_logger._reset_debounce()

    # Control the debounce clock deterministically.
    clock = {"t": 1000.0}
    monkeypatch.setattr(tg_logger.time, "monotonic", lambda: clock["t"])

    class _ErrEvent:
        def __init__(self, exc):
            self.exception = exc

    b = _make_bot()
    err = ValueError("boom")

    # Two rapid identical errors -> one send (debounced).
    r1 = asyncio.run(b.on_error(_ErrEvent(err)))
    r2 = asyncio.run(b.on_error(_ErrEvent(ValueError("boom"))))
    assert r1 is True and r2 is True
    assert len(sent) == 1
    assert sent[0].startswith("🔴 jobhunter error:")

    # Advance past the debounce window -> identical error sends again.
    clock["t"] += tg_logger._DEBOUNCE_WINDOW_S + 1
    asyncio.run(b.on_error(_ErrEvent(ValueError("boom"))))
    assert len(sent) == 2


def test_error_handler_distinct_errors_not_debounced(monkeypatch):
    """Different error messages are NOT debounced against each other."""
    sent = []

    async def fake_send_log(text):
        sent.append(text)

    monkeypatch.setattr(tg_logger, "send_log", fake_send_log)
    tg_logger._reset_debounce()
    monkeypatch.setattr(tg_logger.time, "monotonic", lambda: 5000.0)

    class _ErrEvent:
        def __init__(self, exc):
            self.exception = exc

    b = _make_bot()
    asyncio.run(b.on_error(_ErrEvent(ValueError("one"))))
    asyncio.run(b.on_error(_ErrEvent(ValueError("two"))))
    assert len(sent) == 2


# ---------------------------------------------------------------------------
# Part C — heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_write_creates_int_epoch_file(tmp_path):
    """write_heartbeat writes an int epoch string to the path."""
    path = str(tmp_path / "heartbeat")
    serve.write_heartbeat(path)
    assert os.path.exists(path)
    with open(path) as f:
        content = f.read()
    assert content.isdigit()
    assert int(content) > 1_600_000_000  # a plausible recent epoch


def test_heartbeat_write_swallows_error(monkeypatch, tmp_path):
    """A write failure must NOT raise (liveness file, never crash the loop)."""

    def boom_open(*a, **k):
        raise OSError("disk full")

    # Patch builtins.open as used inside serve.write_heartbeat.
    import builtins

    monkeypatch.setattr(builtins, "open", boom_open)
    # Must not raise.
    serve.write_heartbeat(str(tmp_path / "x"))


def test_heartbeat_loop_writes_once_immediately_then_cancels(tmp_path):
    """heartbeat_loop writes immediately (before the first interval) and can be
    cancelled cleanly."""
    path = str(tmp_path / "heartbeat")

    async def run_briefly():
        task = asyncio.create_task(serve.heartbeat_loop(path, interval=1000))
        # Yield so the task runs its immediate first write.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_briefly())
    assert os.path.exists(path)
    with open(path) as f:
        assert f.read().isdigit()


def test_heartbeat_path_env_override(monkeypatch, tmp_path):
    """HEARTBEAT_PATH overrides the default; default is /tmp/heartbeat."""
    monkeypatch.delenv("HEARTBEAT_PATH", raising=False)
    assert serve.heartbeat_path() == "/tmp/heartbeat"
    custom = str(tmp_path / "hb")
    monkeypatch.setenv("HEARTBEAT_PATH", custom)
    assert serve.heartbeat_path() == custom


def test_serve_amain_starts_and_cancels_heartbeat(pg_dsn, tmp_path, monkeypatch):
    """serve._amain creates the heartbeat under its asyncio.run (file written)
    and cancels it on teardown."""
    hb_path = str(tmp_path / "hb")
    db_path = pg_dsn
    monkeypatch.setenv("HEARTBEAT_PATH", hb_path)

    cfg = Config(
        bot_token=VALID_TOKEN,
        notify_chat_id=123,
        allowed_user_ids={ALLOWED_UID},
        database_url=db_path,
    )

    async def fake_run(self):
        # Let the heartbeat task get a chance to write its immediate tick.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert os.path.exists(hb_path)

    async def fake_aclose(self):
        pass

    monkeypatch.setattr(bot.JobHunterBot, "run", fake_run)
    monkeypatch.setattr(bot.JobHunterBot, "aclose", fake_aclose)

    asyncio.run(serve._amain(cfg))

    # Heartbeat file exists after the run; the task was cancelled in finally.
    assert os.path.exists(hb_path)
    with open(hb_path) as f:
        assert f.read().isdigit()


# ---------------------------------------------------------------------------
# Tests added by the Tester validation pass (gaps not covered by Developer)
# ---------------------------------------------------------------------------


def test_on_error_graceful_when_event_has_no_exception_attr(monkeypatch):
    """Spec: "verify graceful behavior if event has no .exception attr
    (falls back to event)".

    on_error must not raise when handed an object with no .exception attribute;
    it must fall back to the event itself as the error value and still return True.
    """
    sent = []

    async def fake_send_log(text):
        sent.append(text)

    monkeypatch.setattr(tg_logger, "send_log", fake_send_log)
    tg_logger._reset_debounce()

    b = _make_bot()

    # An event object with NO .exception attribute at all.
    class _BareEvent:
        pass

    bare = _BareEvent()
    result = asyncio.run(b.on_error(bare))

    # Must return True (marks error handled) even with no .exception.
    assert result is True
    # Must have called send_log with a string containing repr of the event.
    assert len(sent) == 1
    assert "jobhunter error" in sent[0]


def test_tg_logger_unconfigured_warning_fires_only_once(monkeypatch, caplog):
    """Spec: "NO-OP (single one-time warning)" — calling send_log MULTIPLE TIMES
    when unconfigured must log the warning EXACTLY ONCE, not once per call.

    This tests the _warned_unconfigured guard.
    """
    import logging

    monkeypatch.delenv("TG_LOG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_LOG_CHAT_ID", raising=False)
    monkeypatch.setattr(tg_logger, "_warned_unconfigured", False, raising=False)

    # Also confirm httpx is never called (belt-and-suspenders).
    def boom_client(*a, **k):
        raise AssertionError("httpx must NOT be called when unconfigured")

    monkeypatch.setattr(tg_logger.httpx, "AsyncClient", boom_client)

    with caplog.at_level(logging.WARNING, logger="job_hunter.tg_logger"):
        asyncio.run(tg_logger.send_log("first"))
        asyncio.run(tg_logger.send_log("second"))
        asyncio.run(tg_logger.send_log("third"))

    warning_records = [
        r for r in caplog.records
        if "ops logging not configured" in r.getMessage()
    ]
    assert len(warning_records) == 1, (
        f"Expected exactly 1 warning; got {len(warning_records)}: {[r.getMessage() for r in warning_records]}"
    )


def test_heartbeat_task_is_done_after_amain_completes(pg_dsn, tmp_path, monkeypatch):
    """The heartbeat asyncio.Task must be done (cancelled) after _amain returns.
    An un-cancelled task would be an orphaned task leaking into the next
    asyncio.run() or triggering warnings.
    """
    hb_path = str(tmp_path / "hb_orphan")
    db_path = pg_dsn
    monkeypatch.setenv("HEARTBEAT_PATH", hb_path)

    cfg = Config(
        bot_token=VALID_TOKEN,
        notify_chat_id=123,
        allowed_user_ids={ALLOWED_UID},
        database_url=db_path,
    )

    captured_task = {}

    # Patch heartbeat_loop to capture the task reference from outside.
    real_heartbeat_loop = serve.heartbeat_loop

    async def capturing_heartbeat_loop(path, interval=serve.HEARTBEAT_INTERVAL_S):
        # Record the current task so we can check it after amain exits.
        captured_task["task"] = asyncio.current_task()
        await real_heartbeat_loop(path, interval)

    monkeypatch.setattr(serve, "heartbeat_loop", capturing_heartbeat_loop)

    async def fake_run(self):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def fake_aclose(self):
        pass

    monkeypatch.setattr(bot.JobHunterBot, "run", fake_run)
    monkeypatch.setattr(bot.JobHunterBot, "aclose", fake_aclose)

    asyncio.run(serve._amain(cfg))

    task = captured_task.get("task")
    assert task is not None, "heartbeat_loop task was never started"
    assert task.done(), "heartbeat task must be done (cancelled) after _amain exits — no orphaned tasks"
