"""Telegram ingestion: pure normalization + store_messages with mocked client."""

import asyncio

from job_hunter import ingest_telegram as ing
from job_hunter import store


def test_build_message_link_public():
    assert ing.build_message_link("@chan", 5) == "https://t.me/chan/5"
    assert ing.build_message_link("chan", 5) == "https://t.me/chan/5"


def test_build_message_link_numeric_none():
    assert ing.build_message_link("-100123", 5) is None


def test_normalize_skips_empty():
    assert ing.normalize_message("@c", 1, "   ") is None
    assert ing.normalize_message("@c", 1, None) is None


def test_normalize_ok():
    m = ing.normalize_message("@c", 7, "  hello  ")
    assert m.raw_text == "hello"
    assert m.source_message_id == "7"
    assert m.source_link == "https://t.me/c/7"


def test_store_messages_inserts_and_dedups(conn):
    msgs = [
        ing.IngestMessage("@c", "1", None, "a"),
        ing.IngestMessage("@c", "2", None, "b"),
        ing.IngestMessage("@c", "1", None, "dup"),  # duplicate message id
    ]
    ids = ing.store_messages(conn, msgs)
    assert len(ids) == 2
    assert len(store.list_by_state(conn, "discovered")) == 2


class FakeMsg:
    def __init__(self, mid, text):
        self.id = mid
        self.message = text
        self.text = text


class FakeClient:
    def __init__(self, messages):
        self._messages = messages

    async def iter_messages(self, channel, limit):
        for m in self._messages[:limit]:
            yield m


def test_fetch_channel_messages_mocked():
    client = FakeClient([FakeMsg(1, "hello"), FakeMsg(2, ""), FakeMsg(3, "world")])
    out = asyncio.run(ing.fetch_channel_messages(client, "@c", 10))
    # empty message filtered out
    assert [m.raw_text for m in out] == ["hello", "world"]


def test_ingest_async_with_mock_client(conn, monkeypatch):
    from job_hunter.config import Config

    cfg = Config(
        telegram_api_id=1, telegram_api_hash="h",
        telegram_channels=["@c"], telegram_fetch_limit=10,
    )

    class CtxClient(FakeClient):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    fake = CtxClient([FakeMsg(1, "remote python llm @hr"), FakeMsg(2, "second post")])
    monkeypatch.setattr(ing, "_build_client", lambda c: fake)

    ids = asyncio.run(ing.ingest_async(cfg, conn))
    assert len(ids) == 2
