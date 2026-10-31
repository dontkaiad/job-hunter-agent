"""Secret masking for anything that can reach logs or the ops chat. PURE.

A Telegram Bot API URL embeds the token (``.../bot<id>:<secret>/sendMessage``),
so httpx / urllib exceptions that carry the request URL leak it into text. A DB
DSN embeds the password. Mask both before text is printed, logged or sent.
"""

from __future__ import annotations

import logging
import re

# "bot123456:AAH..." (token inside an API URL) -> "bot***"
_BOT_URL_TOKEN = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")
# bare "123456:AAH..." token
_BARE_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}")
# scheme://user:password@host -> scheme://user:***@host
_DSN_PASSWORD = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s@]+:)[^@\s/]+@")


def redact_secrets(text: object) -> str:
    """Return ``str(text)`` with bot tokens and URL passwords masked."""
    s = str(text)
    s = _BOT_URL_TOKEN.sub("bot***", s)
    s = _BARE_TOKEN.sub("***", s)
    return _DSN_PASSWORD.sub(r"\1***@", s)


def install_log_redaction() -> None:
    """Mask secrets in every ``logging`` record at creation (all loggers/handlers)."""
    old_factory = logging.getLogRecordFactory()
    if getattr(old_factory, "_redacting", False):
        return

    def factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        try:
            record.msg = redact_secrets(record.getMessage())
            record.args = ()
        except Exception:  # noqa: BLE001 - never break logging
            pass
        return record

    factory._redacting = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
