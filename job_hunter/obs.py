"""Observability glue: promote silent warnings + unhandled async errors to ops.

This module is the asyncio/logging GLUE that surfaces failures which otherwise
die quietly (the canonical case: a ``coroutine '...' was never awaited``
RuntimeWarning that fired from GC finalization and went unnoticed for days). It
reuses ``tg_logger.send_error_log`` (the debounced ops-channel alert primitive)
as the dispatch sink.

Why this lives here and NOT in tg_logger
-----------------------------------------
``tg_logger`` is deliberately framework-agnostic: it knows nothing about an
event loop, asyncio, or the Python logging machinery. All the loop-aware /
logging-handler wiring (capturing warnings, scheduling a coroutine onto a
specific loop possibly from OFF that loop) lives HERE so tg_logger stays pure.
``serve`` (and optionally ``run``) call the ``install_*`` helpers at startup,
AFTER the loop is running, passing the running loop.

Spam control
------------
Every alert goes through ``tg_logger.send_error_log``, which DEBOUNCES on the
message text within a 30s window — so a storm of identical "never awaited"
warnings collapses to at most one ops message per window. The staleness check
(below) is wired as a once-daily scheduler job, so it sends at most one alert
per day with no extra dedup state.

Safety
------
A logging handler that itself raises would be strictly worse than the silent
failure it is trying to surface. Every entry point here is wrapped so it can
NEVER raise into its caller (the logging machinery / the event loop).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import tg_logger
from .clock import now_utc

logger = logging.getLogger(__name__)

# The "py.warnings" logger is where ``logging.captureWarnings(True)`` routes
# warnings.warn(...) output (the category + message are formatted into the
# record's message text).
_PY_WARNINGS_LOGGER = "py.warnings"

# Substrings that mark a record as a REAL problem worth an ops alert. We
# deliberately curate this to avoid noise: DeprecationWarning / UserWarning /
# ResourceWarning chatter is NOT forwarded. RuntimeWarning covers the
# never-awaited-coroutine case; the explicit phrase matches cover the same
# class of "you dropped an awaitable / future" bugs even if the category text
# is formatted differently across Python versions.
_FORWARD_MARKERS = (
    "RuntimeWarning",
    "never awaited",
    "never retrieved",
)

# A sentinel attribute we stamp onto our handler so install is idempotent (a
# second install on the same logger does not attach a duplicate handler).
_HANDLER_TAG = "_jobhunter_obs_warning_handler"


def _record_should_forward(message: str) -> bool:
    """PURE: decide whether a formatted py.warnings message warrants an alert.

    Forward only records whose text matches one of the curated markers
    (RuntimeWarning / "never awaited" / "never retrieved"). Everything else —
    DeprecationWarning, UserWarning, ResourceWarning, etc. — is noise and is
    NOT forwarded.
    """
    return any(marker in message for marker in _FORWARD_MARKERS)


def _fire_and_forget(coro) -> None:
    """``ensure_future(coro)`` then RETRIEVE its result so it can never become an
    "unretrieved task exception".

    This matters specifically for the alert path: if ``send_error_log`` ever
    raised, its task would be GC'd unretrieved -> the loop exception handler
    would fire -> it would schedule ANOTHER send -> which raises -> ... an
    unbounded feedback loop. Attaching a done-callback that consumes the
    exception breaks that loop (and silences the "never retrieved" warning) even
    though ``tg_logger.send_error_log`` is itself fully guarded today.
    """
    task = asyncio.ensure_future(coro)

    def _retrieve(t: "asyncio.Future") -> None:
        try:
            if not t.cancelled():
                t.exception()  # retrieve to mark handled; never re-raise
        except Exception:  # noqa: BLE001
            pass

    task.add_done_callback(_retrieve)


def _dispatch_to_loop(loop: asyncio.AbstractEventLoop, text: str) -> None:
    """Schedule ``tg_logger.send_error_log(text)`` onto ``loop``, safely.

    The never-awaited warning fires from GC finalization, which can run on ANY
    thread and OUTSIDE the loop's execution context. ``call_soon_threadsafe``
    is the supported way to hop back onto the loop from another thread; once on
    the loop we ``ensure_future`` the coroutine so it actually runs. The whole
    thing is wrapped so a dispatch failure (e.g. loop already closed) can never
    raise into the logging handler / GC finalizer.
    """
    try:
        def _schedule() -> None:
            try:
                _fire_and_forget(tg_logger.send_error_log(text))
            except Exception:  # noqa: BLE001 — never raise from the loop callback
                pass

        loop.call_soon_threadsafe(_schedule)
    except Exception:  # noqa: BLE001 — loop closed / not running; swallow
        pass


class _WarningAlertHandler(logging.Handler):
    """Logging handler that forwards REAL-problem py.warnings to ops.

    Bound to the serve loop captured at install time. ``emit`` runs
    synchronously and possibly OFF the loop (GC finalizer), so it dispatches the
    async send via ``call_soon_threadsafe``. ``emit`` NEVER raises.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._loop = loop
        setattr(self, _HANDLER_TAG, True)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            message = record.getMessage()
            if not _record_should_forward(message):
                return
            text = f"jobhunter warning: {message}"
            _dispatch_to_loop(self._loop, text)
        except Exception:  # noqa: BLE001 — a logging handler MUST NOT raise
            pass


def install_warning_alerts(loop: asyncio.AbstractEventLoop) -> _WarningAlertHandler:
    """Route real Python warnings to the ops channel as debounced alerts.

    1. ``logging.captureWarnings(True)`` so ``warnings.warn(...)`` is emitted as
       a record on the "py.warnings" logger (category + message in the text).
    2. Attach a handler to that logger that forwards ONLY curated, real-problem
       warnings (RuntimeWarning / never-awaited / never-retrieved) to
       ``tg_logger.send_error_log`` via the captured ``loop``.

    Idempotent: a second call does not attach a duplicate handler. Returns the
    handler (useful for tests / teardown). Never raises.
    """
    try:
        logging.captureWarnings(True)
        py_logger = logging.getLogger(_PY_WARNINGS_LOGGER)
        # Idempotency: don't stack duplicate handlers across re-installs.
        for h in py_logger.handlers:
            if getattr(h, _HANDLER_TAG, False):
                return h  # type: ignore[return-value]
        handler = _WarningAlertHandler(loop)
        # Ensure records at WARNING level (warnings.warn maps to WARNING) flow.
        py_logger.setLevel(logging.WARNING)
        py_logger.addHandler(handler)
        return handler
    except Exception:  # noqa: BLE001 — install must never crash startup
        logger.warning("obs: failed to install warning alerts", exc_info=True)
        # Return a detached handler so callers always get an object back.
        return _WarningAlertHandler(loop)


def install_loop_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Surface unhandled loop/task exceptions to the ops channel.

    Sets a loop exception handler that:
      1. FIRST calls ``loop.default_exception_handler(context)`` so the existing
         stderr logging behaviour is preserved (we ADD an alert, we don't
         replace the default).
      2. THEN builds a message from ``context["message"]`` + the exception repr
         and schedules ``tg_logger.send_error_log(...)`` on the loop (we're
         already on the loop here, so ``ensure_future`` is fine).

    This catches "Task exception was never retrieved" and other unhandled task
    exceptions. The handler NEVER raises.
    """
    def handler(loop_: asyncio.AbstractEventLoop, context: dict) -> None:
        # 1) Preserve the default stderr logging FIRST (never lose the trace).
        try:
            loop_.default_exception_handler(context)
        except Exception:  # noqa: BLE001
            pass
        # 2) Then surface it to ops (debounced). We're on the loop, so
        #    ensure_future is safe; still fully guarded.
        try:
            msg = context.get("message") or "unhandled loop exception"
            exc = context.get("exception")
            text = f"jobhunter loop exception: {msg}"
            if exc is not None:
                text = f"{text}: {exc!r}"
            # Retrieve the send task's result (see _fire_and_forget) so a failing
            # send can't itself become an unretrieved task exception that
            # re-triggers THIS handler in an unbounded loop.
            _fire_and_forget(tg_logger.send_error_log(text))
        except Exception:  # noqa: BLE001 — exception handler MUST NOT raise
            pass

    try:
        loop.set_exception_handler(handler)
    except Exception:  # noqa: BLE001
        logger.warning("obs: failed to install loop exception handler", exc_info=True)


def install_all(loop: asyncio.AbstractEventLoop) -> None:
    """Install BOTH observability hooks on ``loop``. Convenience for entrypoints."""
    install_warning_alerts(loop)
    install_loop_exception_handler(loop)


# --- Harvest staleness watchdog ---------------------------------------------
#
# A SECOND scheduler job (see serve) calls this once a day, the hour after the
# harvest. It reads the harvest heartbeat written to the ops table at the end of
# a completed harvest (run.harvest -> store.set_last_harvest_at) and, if that is
# older than the threshold, sends a LOUD alert to the ops topic.
#
# NOTE on the alert sink: the staleness alert uses ``tg_logger.send_log`` (NOT
# send_error_log) because this is a deliberate, formatted ops-topic line (we
# want the exact "⚠️ jobhunter: ..." string, not the "🔴 jobhunter error: <repr>"
# wrapper). Spam is bounded by the once-daily schedule, not by debounce.

STALE_AFTER_HOURS_DEFAULT = 26


def build_staleness_message(last_harvest_at_iso: str) -> str:
    """PURE: the exact ops alert string for a stale harvest.

    Worded so the FUNCTION-liveness concern is unmistakable and distinct from
    monitor.sh's CONTAINER/process-liveness alerts (which post to the "server"
    ops thread on the host). This fires precisely when the container is healthy
    but the daily harvest has NOT actually run — the exact failure that slipped
    past us — so the wording says so explicitly.
    """
    return (
        f"⚠️ jobhunter: scheduled harvest hasn't run since {last_harvest_at_iso} "
        f"— function-level alert (container liveness is monitor.sh's job)"
    )


async def check_harvest_staleness(
    conn,
    *,
    now=None,
    stale_after_hours: int = STALE_AFTER_HOURS_DEFAULT,
) -> Optional[str]:
    """Read the harvest heartbeat; alert (and return the message) if stale.

    - ``now`` is injectable for tests; defaults to ``clock.now_utc()``.
    - If there is NO heartbeat row yet (fresh deploy, no harvest has completed),
      we do NOT alert and return None — there is no baseline to compare against,
      so alerting would be a deploy-time false positive.
    - If the last harvest is OLDER than ``stale_after_hours``, send the LOUD ops
      alert via ``tg_logger.send_log`` and return the sent message.
    - Otherwise return None (fresh enough).

    This is observability ONLY: it never touches work_items and never calls
    advance(). It reads the ops heartbeat table via store.get_last_harvest_at.
    """
    from . import store  # local import: avoid import cycle at module load

    current = now if now is not None else now_utc()
    last = store.get_last_harvest_at(conn)
    if last is None:
        # No baseline yet (no completed harvest). Do NOT alert on a fresh deploy.
        return None

    age_hours = (current - last).total_seconds() / 3600.0
    if age_hours < stale_after_hours:
        return None

    message = build_staleness_message(last.isoformat())
    await tg_logger.send_log(message)
    return message
