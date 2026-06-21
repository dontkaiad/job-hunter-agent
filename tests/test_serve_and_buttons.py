"""Button UX (handle_callback final-state card edits) + serve entrypoint.

All aiogram / Telegram / LLM I/O is faked here — NO real network, polling, or
auth. The fakes mirror those in test_bot.py (FakeUser / FakeCallback) but the
message stand-in additionally records ``edit_text`` so the final-state card edit
(and its keyboard removal / robustness fallback) can be asserted.
"""

import asyncio
import json

import pytest

from job_hunter import bot, pipeline, serve, store
from job_hunter.config import Config
from job_hunter.pipeline import Deps
from job_hunter.states import APPROVED, BACKLOG, DRAFTED, SKIPPED, SURFACED


ALLOWED_UID = 777


# --- Fakes ------------------------------------------------------------------


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeFinalizeMessage:
    """Message stand-in that records edit_text AND edit_reply_markup.

    Captures the text and reply_markup of the LAST edit_text, plus whether the
    keyboard was stripped, so tests can assert the card was finalised to a
    final-state line with NO inline keyboard.
    """

    def __init__(self, text="🟢 85/100 — AI Eng", html_text=None, edit_raises=False):
        self.text = text
        # aiogram messages expose html_text; handle_callback prefers it.
        self.html_text = html_text if html_text is not None else text
        self._edit_raises = edit_raises
        # Recorded state.
        self.edit_text_calls = 0
        self.last_edit_text = None
        self.last_edit_markup = "UNSET"
        self.markup_cleared = False
        self.reply_markup_removed = False

    async def edit_text(self, text, reply_markup="UNSET", parse_mode=None):
        self.edit_text_calls += 1
        if self._edit_raises:
            # Simulate a non-editable message (too old / deleted / identical).
            raise RuntimeError("message can't be edited")
        self.last_edit_text = text
        self.last_edit_markup = reply_markup
        self.text = text

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup_removed = reply_markup is None
        self.markup_cleared = reply_markup is None


class FakeCallback:
    """callback_query stand-in: records acks; carries a finalize-aware message."""

    def __init__(self, data, user_id=ALLOWED_UID, message=None):
        self.data = data
        self.answered = None
        self.answer_calls = 0
        self.from_user = FakeUser(user_id)
        self.message = message if message is not None else FakeFinalizeMessage()

    async def answer(self, text=None):
        self.answer_calls += 1
        self.answered = text


def _cfg(allowed=None):
    return Config(
        bot_token="x",
        notify_chat_id=123,
        anthropic_api_key=None,
        allowed_user_ids={ALLOWED_UID} if allowed is None else set(allowed),
    )


def _surface_item(conn, deps):
    item_id = store.insert_item(
        conn,
        raw_text="Remote Senior Python LLM RAG Claude FastAPI. 250000-350000 RUB. @hr",
        source_channel="@c",
        source_message_id="1",
    )
    pipeline.run_to_gate(conn, item_id, deps=deps)
    assert store.get_item(conn, item_id).state == SURFACED
    return item_id


# --- Button UX: final-state card edits + keyboard removal -------------------


def test_button_skip_finalizes_card(conn, deps):
    item_id = _surface_item(conn, deps)
    b = bot.JobHunterBot(_cfg(), conn, deps)
    cb = FakeCallback(bot.encode_callback("skip", item_id))

    status = asyncio.run(b.handle_callback(cb))

    assert status == "moved"
    assert store.get_item(conn, item_id).state == SKIPPED
    # Acked.
    assert cb.answer_calls == 1
    assert cb.answered == "Skipped"
    # Card edited to the final-state line WITH the keyboard removed.
    assert cb.message.edit_text_calls == 1
    assert "⏭️ Пропущено" in cb.message.last_edit_text
    assert cb.message.last_edit_markup is None


def test_button_backlog_finalizes_card(conn, deps):
    item_id = _surface_item(conn, deps)
    b = bot.JobHunterBot(_cfg(), conn, deps)
    cb = FakeCallback(bot.encode_callback("backlog", item_id))

    status = asyncio.run(b.handle_callback(cb))

    assert status == "moved"
    assert store.get_item(conn, item_id).state == BACKLOG
    assert cb.answer_calls == 1
    assert cb.answered == "Backlogged"
    assert cb.message.edit_text_calls == 1
    assert "📥 В бэклоге" in cb.message.last_edit_text
    assert cb.message.last_edit_markup is None


def test_button_approve_finalizes_card_and_drives_draft(conn, fake_llm, fake_fx):
    """Approve: SURFACED -> APPROVED, then run_to_gate drives RESEARCHED ->
    DRAFTED with a mocked LLM, the card is finalised to ✅ Принято (keyboard
    removed), and notify_draft sends the draft back."""
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 85, "Обоснование": "ok"}')
    fake_llm.set_for("research", '{"summary":"s","talking_points":[],"questions":[]}')
    fake_llm.set_for("application message", "Hi I want to apply")
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)

    item_id = _surface_item(conn, deps)
    b = bot.JobHunterBot(_cfg(), conn, deps)

    # Capture the draft send WITHOUT touching a real Bot.
    sent = {}

    async def fake_notify_draft(iid):
        sent["id"] = iid

    b.notify_draft = fake_notify_draft  # type: ignore[assignment]

    cb = FakeCallback(bot.encode_callback("approve", item_id))
    status = asyncio.run(b.handle_callback(cb))

    assert status == "moved"
    item = store.get_item(conn, item_id)
    # Approve path drove the LLM pipeline all the way to DRAFTED.
    assert item.state == DRAFTED
    draft_text = json.loads(item.extracted_json)["draft"]
    assert draft_text.startswith("Hi I want to apply")
    assert "github.com/example" in draft_text and "[resume: link]" in draft_text
    # Draft was sent back for review.
    assert sent["id"] == item_id
    # Acked + card finalised to ✅ Принято with the keyboard removed.
    assert cb.answer_calls == 1
    assert cb.answered == "Approved"
    assert cb.message.edit_text_calls == 1
    assert "✅ Принято" in cb.message.last_edit_text
    assert cb.message.last_edit_markup is None


def test_button_non_allowlisted_is_fail_closed(conn, deps, monkeypatch):
    """A non-allowlisted callback returns 'forbidden', NEVER calls advance, does
    not change state, and acks with an EMPTY spinner-stop only."""
    item_id = _surface_item(conn, deps)
    before = store.get_item(conn, item_id).state
    b = bot.JobHunterBot(_cfg(allowed={ALLOWED_UID}), conn, deps)

    called = {"advance": 0}
    monkeypatch.setattr(
        pipeline,
        "advance_by_id",
        lambda *a, **k: called.__setitem__("advance", called["advance"] + 1),
    )

    cb = FakeCallback(bot.encode_callback("approve", item_id), user_id=999)
    status = asyncio.run(b.handle_callback(cb))

    assert status == "forbidden"
    assert called["advance"] == 0
    assert store.get_item(conn, item_id).state == before == SURFACED
    # Empty ack only; no card edit.
    assert cb.answer_calls == 1
    assert cb.answered is None
    assert cb.message.edit_text_calls == 0
    assert cb.message.markup_cleared is False


def test_finalize_card_falls_back_when_edit_text_raises(conn, deps):
    """When message.edit_text raises (non-editable message), _finalize_card
    must NOT raise and must fall back to stripping the keyboard."""
    item_id = _surface_item(conn, deps)
    b = bot.JobHunterBot(_cfg(), conn, deps)
    msg = FakeFinalizeMessage(edit_raises=True)
    cb = FakeCallback(bot.encode_callback("skip", item_id), message=msg)

    # Must not raise.
    status = asyncio.run(b.handle_callback(cb))

    assert status == "moved"
    assert store.get_item(conn, item_id).state == SKIPPED
    # edit_text was attempted and failed; fallback stripped the keyboard.
    assert msg.edit_text_calls == 1
    assert msg.last_edit_text is None  # edit_text body never recorded (it raised)
    assert msg.markup_cleared is True


def test_finalize_card_robust_called_directly(conn, deps):
    """_finalize_card on a raising message does not propagate the exception."""
    b = bot.JobHunterBot(_cfg(), conn, deps)
    msg = FakeFinalizeMessage(edit_raises=True)
    cb = FakeCallback(bot.encode_callback("skip", 1), message=msg)
    # Direct call: should swallow the edit error and strip the keyboard.
    asyncio.run(b._finalize_card(cb, "skip"))
    assert msg.markup_cleared is True


# --- serve entrypoint -------------------------------------------------------


class _ConnProxy:
    """Delegates everything to a real psycopg connection but records close().

    The connection's close attribute cannot always be monkeypatched in place;
    this thin proxy lets tests observe the close call while every other DB
    operation passes straight through.
    """

    def __init__(self, real, on_close):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_on_close", on_close)

    def close(self):
        self._on_close()
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_serve_amain_opens_db_runs_and_tears_down(pg_dsn, monkeypatch):
    """serve._amain opens its OWN DB connection on cfg.database_url, runs init_db,
    uses build_deps, runs the bot (mocked, NO polling), and tears down with
    bot.aclose() + conn.close() in the finally."""
    db_path = pg_dsn
    cfg = Config(
        bot_token="x",
        notify_chat_id=123,
        anthropic_api_key=None,
        allowed_user_ids={ALLOWED_UID},
        database_url=db_path,
    )

    events = []

    # Mock bot.run so NO real polling / network happens.
    async def fake_run(self):
        events.append("run")
        # Prove the bot got a live DB connection that init_db ran on.
        row = self.conn.execute(
            "SELECT to_regclass('public.work_items') AS t"
        ).fetchone()
        assert row is not None and row["t"] is not None, "init_db must have created the schema"

    async def fake_aclose(self):
        events.append("aclose")

    closed = {"conn": False}

    monkeypatch.setattr(bot.JobHunterBot, "run", fake_run)
    monkeypatch.setattr(bot.JobHunterBot, "aclose", fake_aclose)

    # Spy build_deps usage.
    deps_used = {"called": False}
    real_build_deps = serve.build_deps

    def spy_build_deps(c):
        deps_used["called"] = True
        return real_build_deps(c)

    monkeypatch.setattr(serve, "build_deps", spy_build_deps)

    # Spy conn.close via a delegating proxy around the real connection.
    real_connect = store.connect

    def spy_connect(path):
        return _ConnProxy(real_connect(path), lambda: closed.__setitem__("conn", True))

    monkeypatch.setattr(serve.store, "connect", spy_connect)

    asyncio.run(serve._amain(cfg))

    # run executed, then teardown (aclose + conn.close) ran in the finally.
    assert events == ["run", "aclose"]
    assert deps_used["called"] is True
    assert closed["conn"] is True


def test_serve_amain_tears_down_on_keyboard_interrupt(pg_dsn, monkeypatch):
    """If bot.run raises (e.g. cancellation / Ctrl-C), the finally still closes
    the session AND the DB connection (resources never leak)."""
    db_path = pg_dsn
    cfg = Config(
        bot_token="x",
        notify_chat_id=123,
        anthropic_api_key=None,
        allowed_user_ids={ALLOWED_UID},
        database_url=db_path,
    )

    events = []
    closed = {"conn": False}

    async def boom_run(self):
        events.append("run")
        raise KeyboardInterrupt

    async def fake_aclose(self):
        events.append("aclose")

    monkeypatch.setattr(bot.JobHunterBot, "run", boom_run)
    monkeypatch.setattr(bot.JobHunterBot, "aclose", fake_aclose)

    real_connect = store.connect

    def spy_connect(path):
        return _ConnProxy(real_connect(path), lambda: closed.__setitem__("conn", True))

    monkeypatch.setattr(serve.store, "connect", spy_connect)

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(serve._amain(cfg))

    # Teardown ran despite the exception.
    assert events == ["run", "aclose"]
    assert closed["conn"] is True


def test_serve_main_wraps_single_asyncio_run_and_swallows_keyboard_interrupt(monkeypatch):
    """serve.main wraps exactly one asyncio.run and handles KeyboardInterrupt
    cleanly (the process exits without propagating it)."""
    calls = {"asyncio_run": 0}

    async def fake_amain():
        # _amain itself would re-raise KeyboardInterrupt out of asyncio.run.
        raise KeyboardInterrupt

    monkeypatch.setattr(serve, "_amain", fake_amain)

    real_asyncio_run = asyncio.run

    def counting_run(coro):
        calls["asyncio_run"] += 1
        return real_asyncio_run(coro)

    monkeypatch.setattr(serve.asyncio, "run", counting_run)

    # Must NOT raise: main swallows KeyboardInterrupt.
    serve.main()

    assert calls["asyncio_run"] == 1


def test_serve_amain_requires_bot_token_and_chat(tmp_path, monkeypatch):
    """_amain fails fast (cfg.require) when bot_token / notify_chat_id missing,
    before opening any DB connection or constructing the bot."""
    cfg = Config(bot_token=None, notify_chat_id=None,
                 database_url="postgresql://u:p@localhost:5432/x")

    connected = {"called": False}
    real_connect = store.connect

    def spy_connect(path):
        connected["called"] = True
        return real_connect(path)

    monkeypatch.setattr(serve.store, "connect", spy_connect)

    with pytest.raises(RuntimeError):
        asyncio.run(serve._amain(cfg))

    # Failed before opening the DB.
    assert connected["called"] is False
