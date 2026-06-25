"""Unit tests for ResilientConn and ensure_reconnected.

Strategy: fake psycopg connections (closed/open flag), patch _raw_connect and
tg_logger.send_log. No real Postgres needed.

Coverage:
  T1  live execute passes through, no reconnect triggered
  T2  execute on dead inner conn → reconnect + retry → success
  T3  non-connection errors (e.g. bad SQL) are re-raised unchanged
  T4  all _raw_connect attempts fail → raises after MAX_RETRIES, prints error
  T5  ensure_reconnected (async) — live conn → noop, no log
  T6  ensure_reconnected (async) — closed conn → reconnects + tg_logger warning
  T7  closure cell sharing: nonlocal conn rebind visible across closures
"""

from __future__ import annotations

import asyncio

import pytest

from job_hunter import store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal psycopg connection stand-in."""

    def __init__(self, closed: int = 0) -> None:
        self.closed = closed
        self._execute_calls: list = []

    def execute(self, sql, params=None):
        if self.closed:
            raise _FakeConnError("connection is closed")
        self._execute_calls.append((sql, params))
        return self

    def fetchone(self):
        return None

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _FakeConnError(store.psycopg.InterfaceError):
    pass


def _run(coro):
    return asyncio.run(coro)


def _resilient(inner, dsn="postgresql://x") -> store.ResilientConn:
    return store.ResilientConn(inner, dsn)


# ---------------------------------------------------------------------------
# T1 — live execute passes through unchanged, _raw_connect never called
# ---------------------------------------------------------------------------


def test_execute_live_conn_passthrough(monkeypatch):
    raw_calls = []
    monkeypatch.setattr("job_hunter.store._raw_connect", lambda dsn: raw_calls.append(dsn))

    live = _FakeConn(closed=0)
    rc = _resilient(live)
    rc.execute("SELECT 1")

    assert live._execute_calls == [("SELECT 1", None)]
    assert raw_calls == [], "_raw_connect must not be called for a live conn"


# ---------------------------------------------------------------------------
# T2 — execute on dead inner conn → reconnect + retry → success
# ---------------------------------------------------------------------------


def test_execute_reconnects_on_connection_error(monkeypatch):
    fresh = _FakeConn(closed=0)
    monkeypatch.setattr("job_hunter.store._raw_connect", lambda dsn: fresh)
    monkeypatch.setattr("job_hunter.store._RECONNECT_BACKOFF_BASE", 0.0)

    dead = _FakeConn(closed=1)
    rc = _resilient(dead)
    rc.execute("SELECT 1", (42,))

    assert rc._inner is fresh, "ResilientConn must swap _inner to fresh conn"
    assert fresh._execute_calls == [("SELECT 1", (42,))]


# ---------------------------------------------------------------------------
# T3 — non-connection errors are re-raised, no reconnect attempted
# ---------------------------------------------------------------------------


def test_execute_reraises_non_connection_errors(monkeypatch):
    raw_calls = []
    monkeypatch.setattr("job_hunter.store._raw_connect", lambda dsn: raw_calls.append(dsn))

    class _BadConn:
        closed = 0
        def execute(self, sql, params=None):
            raise ValueError("bad SQL syntax")

    rc = _resilient(_BadConn())
    with pytest.raises(ValueError, match="bad SQL"):
        rc.execute("bad")

    assert raw_calls == [], "_raw_connect must not be called for non-conn errors"


# ---------------------------------------------------------------------------
# T4 — all reconnect attempts fail → raises after MAX_RETRIES
# ---------------------------------------------------------------------------


def test_sync_reconnect_retries_then_raises(monkeypatch):
    connect_calls: list[str] = []

    def failing_connect(dsn: str):
        connect_calls.append(dsn)
        raise OSError("connection refused")

    monkeypatch.setattr("job_hunter.store._raw_connect", failing_connect)
    monkeypatch.setattr("job_hunter.store._RECONNECT_BACKOFF_BASE", 0.0)

    dead = _FakeConn(closed=1)
    rc = _resilient(dead)

    with pytest.raises(OSError, match="connection refused"):
        rc.execute("SELECT 1")

    assert len(connect_calls) == store._RECONNECT_MAX_RETRIES, (
        f"expected {store._RECONNECT_MAX_RETRIES} attempts, got {len(connect_calls)}"
    )


# ---------------------------------------------------------------------------
# T5 — ensure_reconnected (async): live conn → returned as-is, no log
# ---------------------------------------------------------------------------


def test_ensure_reconnected_live_conn_is_noop(monkeypatch):
    raw_calls = []
    monkeypatch.setattr("job_hunter.store._raw_connect", lambda dsn: raw_calls.append(dsn))

    logs: list[str] = []

    async def fake_send_log(text):
        logs.append(text)

    monkeypatch.setattr("job_hunter.tg_logger.send_log", fake_send_log)

    live = _FakeConn(closed=0)
    rc = _resilient(live)
    result = _run(store.ensure_reconnected(rc, "postgresql://x"))

    assert result is rc
    assert raw_calls == []
    assert logs == [], "no tg_logger call expected for live conn"


# ---------------------------------------------------------------------------
# T6 — ensure_reconnected (async): closed conn → reconnects + tg_logger ⚠️
# ---------------------------------------------------------------------------


def test_ensure_reconnected_closed_conn_reconnects_and_logs(monkeypatch):
    fresh = _FakeConn(closed=0)
    monkeypatch.setattr("job_hunter.store._raw_connect", lambda dsn: fresh)
    monkeypatch.setattr("job_hunter.store._RECONNECT_BACKOFF_BASE", 0.0)

    logs: list[str] = []

    async def fake_send_log(text):
        logs.append(text)

    monkeypatch.setattr("job_hunter.tg_logger.send_log", fake_send_log)

    dead = _FakeConn(closed=1)
    rc = _resilient(dead)
    result = _run(store.ensure_reconnected(rc, "postgresql://x"))

    assert result is rc, "same ResilientConn object returned (reconnected in-place)"
    assert rc._inner is fresh, "_inner must be swapped to fresh conn"
    assert len(logs) == 1
    assert "⚠️" in logs[0]
    assert "reconnect" in logs[0].lower()


# ---------------------------------------------------------------------------
# T7 — closure cell sharing: nonlocal rebind visible across closures
# ---------------------------------------------------------------------------


def test_closure_nonlocal_rebind_shared_across_closures(monkeypatch):
    """After 'nonlocal conn; conn = await store.ensure_reconnected(conn, dsn)'
    in one closure, other closures that close over the same cell see the update.
    (This verifies the serve.py pattern remains correct with ResilientConn.)
    """
    fresh = _FakeConn(closed=0)
    monkeypatch.setattr("job_hunter.store._raw_connect", lambda dsn: fresh)
    monkeypatch.setattr("job_hunter.store._RECONNECT_BACKOFF_BASE", 0.0)

    logs: list[str] = []

    async def fake_send_log(text):
        logs.append(text)

    monkeypatch.setattr("job_hunter.tg_logger.send_log", fake_send_log)

    dead = _FakeConn(closed=1)
    conn = _resilient(dead)
    observed: list = []

    async def closure_a():
        nonlocal conn
        conn = await store.ensure_reconnected(conn, "postgresql://x")

    async def closure_b():
        observed.append(conn._inner)

    async def run():
        await closure_a()
        await closure_b()

    _run(run())

    assert conn._inner is fresh, "cell must be rebound to fresh inner"
    assert observed == [fresh], "closure_b must see the reconnected inner"
