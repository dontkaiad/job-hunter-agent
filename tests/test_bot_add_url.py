"""Tests for the bot's add-by-URL handler + score-band routing.

``_deliver_by_band`` is verified directly against seeded (state, score) items so
each band is isolated; ``handle_url_message`` is verified against a monkeypatched
add_by_url so the dispatch logic is tested without network/LLM. The bot's real
aiogram Bot is replaced by a capture stub (no network).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from job_hunter import add_by_url as add_by_url_mod
from job_hunter import bot, store
from job_hunter.config import Config
from job_hunter.pipeline import Deps
from job_hunter.states import REJECTED, SURFACED

ALLOWED_UID = 777


class _CaptureBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append({"chat_id": chat_id, "text": text, **kwargs})


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, text, uid=ALLOWED_UID):
        self.text = text
        self.from_user = _FakeUser(uid)
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


def _cfg():
    return Config(
        bot_token="x", notify_chat_id=123, anthropic_api_key=None,
        allowed_user_ids={ALLOWED_UID},
    )


def _bot(conn, deps):
    b = bot.JobHunterBot(_cfg(), conn, deps)
    b._bot = _CaptureBot()
    b._ensure = lambda: None  # type: ignore[assignment]
    return b


def _seed(conn, *, state, score):
    """Insert an item and drive it to (state, score) via the store directly."""
    item_id = store.insert_item(
        conn, raw_text="Some role. Python.", source_channel="manual",
        source_message_id=f"seed-{state}-{score}",
    )
    ex = {"title": "Eng", "stack": ["python"], "reasons": ["почему-то"],
          "Обоснование": "почему-то"}
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex), relevance_score=float(score))
    store.update_state(conn, item_id, state, from_state="extracted",
                       kind="deterministic", actor="system",
                       relevance_score=float(score))
    return item_id


# --- pure: URL extraction ---------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("https://career.example.com/jobs/1", "https://career.example.com/jobs/1"),
        ("посмотри: https://hh.ru/vacancy/42 спасибо", "https://hh.ru/vacancy/42"),
        ("https://t.me/jobs/9", "https://t.me/jobs/9"),
        ("/borderline", None),
        ("just chatter", None),
        (None, ""),
    ],
)
def test_first_url(text, expected):
    # None expected for non-URL; the None input returns None too.
    got = bot.first_url(text)
    assert got == (expected or None)


# --- band routing -----------------------------------------------------------


def test_deliver_surfaced_uses_notify_surfaced(conn, deps):
    b = _bot(conn, deps)
    item_id = _seed(conn, state=SURFACED, score=88)
    called = {"n": 0}

    async def _fake_notify(iid):
        called["n"] += 1
        assert iid == item_id

    b.notify_surfaced = _fake_notify  # type: ignore[assignment]
    asyncio.run(b._deliver_by_band(item_id))
    assert called["n"] == 1
    # notify_surfaced (faked) owns the send; no raw send_message here.
    assert b._bot.calls == []


def test_deliver_borderline_uses_borderline_renderer(conn, deps):
    b = _bot(conn, deps)
    item_id = _seed(conn, state=REJECTED, score=55)  # borderline band, rejected state
    asyncio.run(b._deliver_by_band(item_id))
    assert len(b._bot.calls) == 1
    text = b._bot.calls[0]["text"]
    # render_borderline output: score truncated to 55 and no surfaced buttons.
    assert "55" in text
    assert "reply_markup" not in b._bot.calls[0]


def test_deliver_rejected_one_line(conn, deps):
    b = _bot(conn, deps)
    item_id = _seed(conn, state=REJECTED, score=31)
    asyncio.run(b._deliver_by_band(item_id))
    assert len(b._bot.calls) == 1
    text = b._bot.calls[0]["text"]
    assert text.startswith("отклонено: score 31")
    assert "reply_markup" not in b._bot.calls[0]


# --- handle_url_message dispatch -------------------------------------------


def _patch_outcome(monkeypatch, outcome):
    monkeypatch.setattr(
        add_by_url_mod, "add_by_url", lambda *a, **k: outcome
    )


def test_handle_no_url_is_silent(conn, deps, monkeypatch):
    b = _bot(conn, deps)
    seen = {"called": False}
    monkeypatch.setattr(
        add_by_url_mod, "add_by_url",
        lambda *a, **k: seen.update(called=True),
    )
    msg = _FakeMessage("just a note, no link")
    asyncio.run(b.handle_url_message(msg))
    assert seen["called"] is False  # never invoked the flow
    assert msg.answers == []  # no reply spam


def test_handle_duplicate_reply(conn, deps, monkeypatch):
    b = _bot(conn, deps)
    _patch_outcome(
        monkeypatch,
        add_by_url_mod.AddOutcome("duplicate", item_id=7, state="surfaced", score=80),
    )
    msg = _FakeMessage("https://x.example/jobs/1")
    asyncio.run(b.handle_url_message(msg))
    assert any("уже в пайплайне" in a for a in msg.answers)
    assert b._bot.calls == []  # no card delivered


def test_handle_unreadable_reply(conn, deps, monkeypatch):
    b = _bot(conn, deps)
    _patch_outcome(monkeypatch, add_by_url_mod.AddOutcome("unreadable", reason="x"))
    msg = _FakeMessage("https://x.example/js")
    asyncio.run(b.handle_url_message(msg))
    assert any("не удалось прочитать" in a.lower() for a in msg.answers)


def test_handle_added_routes_to_band(conn, deps, monkeypatch):
    b = _bot(conn, deps)
    _patch_outcome(
        monkeypatch,
        add_by_url_mod.AddOutcome("added", item_id=42, state="surfaced", score=90),
    )
    routed = {"id": None}

    async def _fake_band(iid):
        routed["id"] = iid

    b._deliver_by_band = _fake_band  # type: ignore[assignment]
    asyncio.run(b.handle_url_message(_FakeMessage("https://x.example/jobs/2")))
    assert routed["id"] == 42
