"""Ops-channel Telegram logger (runbook pattern, mirrors Nexus/Arcana).

Posts short lifecycle/error lines to a SEPARATE "ops" Telegram bot + chat +
topic via a RAW Bot API HTTP call (httpx) — it deliberately does NOT spin up an
aiogram Bot (that one is for the operator-facing HITL surface; this is plumbing).

Design contract:
  * GRACEFUL DEGRADATION. If the bot token or chat id is missing/empty the whole
    thing is a NO-OP: it logs a single one-time warning and returns. It NEVER
    raises into the caller, and a Telegram/network failure is swallowed (logged
    as a warning) too — ops logging must never crash the bot it is reporting on.
  * Self-contained / zero side effects at import time. Config is read from
    os.environ on each call (cheap), so importing this module starts nothing.
  * thread id is OPTIONAL: when TG_LOG_THREAD_JOBHUNTER is unset the
    message_thread_id field is simply omitted (posts to the chat's General).

Env vars (see .env.example):
  TG_LOG_BOT_TOKEN        — ops bot token (required to send)
  TG_LOG_CHAT_ID          — ops chat/supergroup id (required to send)
  TG_LOG_THREAD_JOBHUNTER — ops topic thread id (optional; e.g. 30)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"

# One-time "ops logging not configured" warning guard, so a serve process that
# runs without the ops vars does not spam the log on every send_log call.
_warned_unconfigured = False

# --- Error-handler debounce -------------------------------------------------
#
# The aiogram global error handler can fire rapidly (e.g. a flapping update).
# Debounce so identical/rapid error lines do not flood the ops channel: at most
# one error send per _DEBOUNCE_WINDOW_S, deduped on the message text. State is a
# module-level pair keyed by message; uses time.monotonic() (allowed — this is
# liveness/rate state, not a stored business timestamp).
_DEBOUNCE_WINDOW_S = 30.0
_last_sent_text: Optional[str] = None
_last_sent_monotonic: float = 0.0


def _reset_debounce() -> None:
    """Test hook: clear the debounce state."""
    global _last_sent_text, _last_sent_monotonic
    _last_sent_text = None
    _last_sent_monotonic = 0.0


def _should_send(text: str, *, now: Optional[float] = None) -> bool:
    """Debounce gate. PURE-ish (reads/writes module state, clock injectable).

    Returns True (and records the send) when ``text`` is NEW or the debounce
    window has elapsed since the last identical send; False to suppress. The
    clock is injectable via ``now`` so tests need no real sleep.
    """
    global _last_sent_text, _last_sent_monotonic
    t = time.monotonic() if now is None else now
    if text == _last_sent_text and (t - _last_sent_monotonic) < _DEBOUNCE_WINDOW_S:
        return False
    _last_sent_text = text
    _last_sent_monotonic = t
    return True


def _config(env: Optional[dict] = None):
    """Read (token, chat_id, thread_id) from the environment. PURE given env."""
    e = os.environ if env is None else env
    token = (e.get("TG_LOG_BOT_TOKEN") or "").strip() or None
    chat_id = (e.get("TG_LOG_CHAT_ID") or "").strip() or None
    thread_id = (e.get("TG_LOG_THREAD_JOBHUNTER") or "").strip() or None
    return token, chat_id, thread_id


async def send_log(text: str) -> None:
    """Post ``text`` to the ops chat/thread via the raw Bot API. NEVER raises.

    No-ops (with a single one-time warning) when the token or chat id is unset.
    Any network / Telegram error is caught and logged as a warning. Returns None
    always so callers can `await send_log(...)` fire-and-(safely)-forget.
    """
    global _warned_unconfigured
    token, chat_id, thread_id = _config()

    if not token or not chat_id:
        if not _warned_unconfigured:
            _warned_unconfigured = True
            logger.warning(
                "tg_logger: ops logging not configured "
                "(TG_LOG_BOT_TOKEN / TG_LOG_CHAT_ID unset); skipping ops logs"
            )
        return

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = thread_id

    url = _API.format(token=token)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — ops logging must never crash caller
        logger.warning("tg_logger: failed to send ops log: %r", exc)
        return


async def send_error_log(err: object) -> None:
    """Send a debounced error line to the ops channel. NEVER raises.

    Identical/rapid errors within the debounce window are suppressed so a
    flapping update cannot flood the ops topic.
    """
    text = f"\U0001f534 jobhunter error: {err!r}"
    if not _should_send(text):
        return
    await send_log(text)
