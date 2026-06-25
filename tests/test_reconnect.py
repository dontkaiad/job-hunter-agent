"""Unit tests for serve._ensure_reconnected (DB auto-reconnect on job startup).

Strategy: fake psycopg connections (closed/open flag), mock store.connect and
tg_logger.send_log. No real Postgres needed.
"""

from __future__ import annotations

import asyncio

import pytest

from job_hunter import serve


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal psycopg connection stand-in: just the .closed attribute."""

    def __init__(self, closed: int = 0) -> None:
        self.closed = closed


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# T1 — live conn passes through unchanged
# ---------------------------------------------------------------------------


def test_ensure_reconnected_live_conn_is_noop(monkeypatch):
    """conn.closed == 0 → returned as-is; store.connect never called."""
    live = _FakeConn(closed=0)
    connect_calls = []
    monkeypatch.setattr("job_hunter.serve.store.connect", lambda dsn: connect_calls.append(dsn) or _FakeConn())

    result = _run(serve._ensure_reconnected(live, "postgresql://x"))

    assert result is live
    assert connect_calls == [], "store.connect must not be called for a live conn"


# ---------------------------------------------------------------------------
# T2 — closed conn → reconnect → ops WARNING → new conn returned
# ---------------------------------------------------------------------------


def test_ensure_reconnected_reconnects_closed_conn(monkeypatch):
    """conn.closed != 0 → store.connect called, ops warning sent, new conn returned."""
    dead = _FakeConn(closed=1)
    fresh = _FakeConn(closed=0)

    sent_logs: list[str] = []

    async def fake_send_log(text: str) -> None:
        sent_logs.append(text)

    monkeypatch.setattr("job_hunter.serve.store.connect", lambda dsn: fresh)
    monkeypatch.setattr("job_hunter.tg_logger.send_log", fake_send_log)

    result = _run(serve._ensure_reconnected(dead, "postgresql://testdsn"))

    assert result is fresh, "must return the new connection"
    assert len(sent_logs) == 1
    assert "⚠️" in sent_logs[0]
    assert "reconnected" in sent_logs[0]


# ---------------------------------------------------------------------------
# T3 — all retries fail → error log + re-raise
# ---------------------------------------------------------------------------


def test_ensure_reconnected_retries_then_raises(monkeypatch):
    """On repeated store.connect failures: retries MAX_RETRIES times, sends
    error log, then re-raises the last exception."""
    dead = _FakeConn(closed=1)

    connect_calls: list[str] = []
    sent_logs: list[str] = []

    def failing_connect(dsn: str) -> None:
        connect_calls.append(dsn)
        raise OSError("connection refused")

    async def fake_send_log(text: str) -> None:
        sent_logs.append(text)

    monkeypatch.setattr("job_hunter.serve.store.connect", failing_connect)
    monkeypatch.setattr("job_hunter.tg_logger.send_log", fake_send_log)
    monkeypatch.setattr("job_hunter.serve._RECONNECT_BACKOFF_BASE", 0.0)  # no sleep in tests

    with pytest.raises(OSError, match="connection refused"):
        _run(serve._ensure_reconnected(dead, "postgresql://testdsn"))

    assert len(connect_calls) == serve._RECONNECT_MAX_RETRIES, (
        f"expected {serve._RECONNECT_MAX_RETRIES} attempts, got {len(connect_calls)}"
    )
    assert any("🔴" in log or "failed" in log.lower() for log in sent_logs), (
        f"expected a loud error log; got: {sent_logs}"
    )


# ---------------------------------------------------------------------------
# T4 — closure cell sharing: nonlocal conn rebind is visible to all closures
# ---------------------------------------------------------------------------


def test_closure_nonlocal_rebind_shared_across_closures(monkeypatch):
    """Verify the Python closure-cell pattern used in _amain works as intended:
    after 'nonlocal conn; conn = new_conn' in one closure, other closures that
    close over the same cell see the updated value."""
    dead = _FakeConn(closed=1)
    fresh = _FakeConn(closed=0)

    sent_logs: list[str] = []

    async def fake_send_log(text: str) -> None:
        sent_logs.append(text)

    monkeypatch.setattr("job_hunter.serve.store.connect", lambda dsn: fresh)
    monkeypatch.setattr("job_hunter.tg_logger.send_log", fake_send_log)

    conn = dead  # shared cell in the enclosing scope

    class _FakeBot:
        conn = dead

    bot = _FakeBot()
    observed_in_closure_b: list = []

    async def closure_a() -> None:
        """Simulates _scheduled_harvest: reconnects and rebinds the shared cell."""
        nonlocal conn
        conn = await serve._ensure_reconnected(conn, "postgresql://x")
        bot.conn = conn

    async def closure_b() -> None:
        """Simulates _scheduled_staleness_check: reads the (now updated) cell."""
        observed_in_closure_b.append(conn)

    async def run() -> None:
        await closure_a()
        await closure_b()

    _run(run())

    assert conn is fresh, "enclosing-scope cell must be rebound to fresh"
    assert bot.conn is fresh, "bot.conn must be updated to fresh"
    assert observed_in_closure_b == [fresh], "closure_b must see the new conn"
