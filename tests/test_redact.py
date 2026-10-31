"""Bot tokens and DB passwords must never reach logs or the ops chat."""
import asyncio
import logging

import httpx

from job_hunter import tg_logger
from job_hunter.redact import install_log_redaction, redact_secrets

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnopq"


def test_redacts_bot_url_token_and_dsn_password():
    s = redact_secrets(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage failed; "
                       "db=postgresql://user:s3cr3t@host:5432/db")
    assert TOKEN not in s and "s3cr3t" not in s
    assert "bot***/sendMessage" in s and "user:***@host" in s


def test_plain_text_untouched():
    assert redact_secrets("harvest 12:30 ok, 5 items") == "harvest 12:30 ok, 5 items"


def test_log_records_are_redacted(caplog):
    install_log_redaction()
    logger = logging.getLogger("redact_test")
    with caplog.at_level(logging.WARNING, logger="redact_test"):
        logger.warning("boom %s", f"https://api.telegram.org/bot{TOKEN}/x")
    assert TOKEN not in caplog.text and "bot***" in caplog.text


def test_send_log_failure_does_not_leak_token(monkeypatch, caplog):
    monkeypatch.setenv("TG_LOG_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TG_LOG_CHAT_ID", "1")

    class Boom:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None):
            raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(tg_logger.httpx, "AsyncClient", Boom)
    with caplog.at_level(logging.WARNING):
        asyncio.run(tg_logger.send_log("hello"))
    assert TOKEN not in caplog.text


def test_send_log_masks_token_inside_message(monkeypatch):
    sent = {}
    monkeypatch.setenv("TG_LOG_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TG_LOG_CHAT_ID", "1")

    class Ok:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None):
            sent["text"] = json["text"]
            class R:
                def raise_for_status(self): pass
            return R()

    monkeypatch.setattr(tg_logger.httpx, "AsyncClient", Ok)
    asyncio.run(tg_logger.send_log(f"err https://api.telegram.org/bot{TOKEN}/x"))
    assert TOKEN not in sent["text"]
