"""Bot pure helpers + handle_callback wiring with mocked aiogram objects."""

import asyncio
import json

from job_hunter import bot, pipeline, store
from job_hunter.config import Config
from job_hunter.pipeline import Deps
from job_hunter.states import APPROVED, BACKLOG, DRAFTED, SKIPPED, SURFACED


# --- Pure helpers ----------------------------------------------------------


def test_encode_decode_callback():
    data = bot.encode_callback("approve", 42)
    assert bot.decode_callback(data) == ("approve", 42)
    assert bot.decode_callback("bad") is None
    assert bot.decode_callback("jh:approve:notint") is None


def test_render_surfaced_includes_score_and_salary(conn):
    item_id = store.insert_item(conn, raw_text="x", source_channel="@c", source_message_id="1")
    ex = {"title": "AI Eng", "stack": ["python", "llm"], "salary_min": 2000,
          "salary_max": 3000, "currency": "EUR", "remote": True, "reasons": ["llm +"]}
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex), relevance_score=72.0)
    item = store.get_item(conn, item_id)
    text = bot.render_surfaced(item, salary_rub=300000)
    # New header: emoji + score/100 + title.
    assert "72/100" in text
    assert "AI Eng" in text
    assert "~300k ₽" in text
    assert "python, llm" in text
    # No old +N score-math display.
    assert "+8" not in text and "+10" not in text


def test_render_draft(conn):
    item_id = store.insert_item(conn, raw_text="x", source_channel="@c", source_message_id="1")
    ex = {"title": "AI Eng", "draft": "Hello there"}
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system", extracted_json=json.dumps(ex))
    item = store.get_item(conn, item_id)
    text = bot.render_draft(item)
    assert "Hello there" in text


def test_keyboard_specs():
    spec = bot.surfaced_keyboard_spec(7)
    actions = [bot.decode_callback(cb)[0] for _, cb, _ in spec]
    assert actions == ["approve", "backlog", "skip"]
    # Draft gate: the only state-affecting button is the manual-confirm
    # «✅ Отправила» (operator taps it AFTER sending the отклик by hand). It
    # keeps the stable "send" action token -> DECISION_SEND -> T12.
    dspec = bot.draft_keyboard_spec(7)
    assert bot.decode_callback(dspec[0][1])[0] == "send"
    assert dspec[0][0] == "✅ Отправила"


# --- Card: language + emoji band + Обоснование (Part 2) --------------------


def test_is_russian_pure():
    assert bot.is_russian("AI инженер") is True
    assert bot.is_russian("AI Engineer") is False
    assert bot.is_russian("") is False
    assert bot.is_russian(None) is False


def test_score_emoji_bands():
    assert bot.score_emoji(90) == "🟢"
    assert bot.score_emoji(65) == "🟡"
    assert bot.score_emoji(40) == "🔴"
    assert bot.score_emoji(None) == "🔴"


def test_russian_post_renders_russian_labels_and_reasoning(conn):
    item_id = store.insert_item(
        conn,
        raw_text="Удалённый AI инженер. Python, LLM, RAG. Зарплата 250000 RUB. @hr_acme",
        source_channel="@jobschan", source_message_id="ru1",
        source_link="https://t.me/jobschan/55",
    )
    ex = {
        "title": "AI инженер",
        "company": "Акме",
        "stack": ["python", "llm", "rag"],
        "seniority": "middle",
        "remote": True,
        "salary_min": 250000, "salary_max": 250000, "currency": "RUB",
        "contact": "@hr_acme", "contact_type": "dm",
        "source_channel": "@jobschan",
        "source_link": "https://t.me/jobschan/55",
        "Обоснование": "Роль про внедрение LLM, удалёнка — высокий скор.",
    }
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex), relevance_score=82.0)
    item = store.get_item(conn, item_id)
    text = bot.render_surfaced(item, salary_rub=250000)

    # Emoji header by band (82 -> green) + score/100 + title.
    assert text.startswith("🟢 82/100 — AI инженер")
    # Russian labels, now BOLD via Telegram HTML.
    assert "<b>Компания</b>:" in text
    assert "<b>Стек</b>:" in text
    assert "<b>Уровень</b>:" in text
    assert "<b>Зарплата</b>:" in text
    assert "<b>Контакт</b>" in text
    assert "<b>Источник</b>:" in text
    # Обоснование block with the Sonnet rationale, in Russian.
    assert "💭 Обоснование:" in text
    assert "внедрение LLM" in text
    # Salary shows ORIGINAL currency AND ₽-equiv.
    salary_line = next(l for l in text.splitlines() if "Зарплата" in l)
    assert "RUB" in salary_line and "₽" in salary_line
    # Open-original link.
    assert "🔗 Открыть оригинал" in text
    assert "https://t.me/jobschan/55" in text
    # No old +N math.
    assert "+8" not in text and "+10" not in text


def test_english_post_renders_english_labels(conn):
    item_id = store.insert_item(
        conn, raw_text="Remote AI Engineer. Python, LLM. @hr",
        source_channel="@jobs", source_message_id="en1",
        source_link="https://t.me/jobs/7",
    )
    ex = {
        "title": "AI Engineer", "stack": ["python", "llm"], "remote": True,
        "salary_min": 3000, "salary_max": 3000, "currency": "EUR",
        "source_channel": "@jobs", "source_link": "https://t.me/jobs/7",
        "Обоснование": "Applied-LLM role, remote -- strong fit.",
    }
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex), relevance_score=70.0)
    item = store.get_item(conn, item_id)
    text = bot.render_surfaced(item, salary_rub=290000)
    assert text.startswith("🟡 70/100 — AI Engineer")
    # English labels, now BOLD via Telegram HTML.
    assert "<b>Stack</b>:" in text
    assert "<b>Salary</b>:" in text
    assert "💭 Rationale:" in text
    assert "🔗 Open original" in text
    salary_line = next(l for l in text.splitlines() if "Salary" in l)
    assert "EUR" in salary_line and "₽" in salary_line


def test_card_escapes_dynamic_values_to_not_break_html(conn):
    """Dynamic field values containing < & > must be HTML-escaped so they don't
    break the HTML parse_mode markup (and bold labels stay intact)."""
    item_id = store.insert_item(
        conn, raw_text="AI Engineer role with C++ & <stuff>",
        source_channel="@jobs", source_message_id="esc1",
        source_link="https://t.me/jobs/9",
    )
    ex = {
        "title": "AI Engineer <lead> & co",
        "company": "Acme <b>Corp</b> & Sons",
        "stack": ["c++", "a<b>", "r&d"],
        "remote": True,
        "source_channel": "@jobs", "source_link": "https://t.me/jobs/9",
        "Обоснование": "Strong fit (88)\n✅ Owns RAG & routing\n⚠️ <english> rusty",
    }
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex), relevance_score=88.0)
    item = store.get_item(conn, item_id)
    text = bot.render_surfaced(item)

    # Raw dangerous sequences from VALUES must NOT appear unescaped.
    assert "<lead>" not in text
    assert "<b>Corp</b>" not in text
    assert "a<b>" not in text
    assert "<english>" not in text
    # They appear escaped instead.
    assert "&lt;lead&gt;" in text
    assert "Acme &lt;b&gt;Corp&lt;/b&gt; &amp; Sons" in text
    assert "&amp;" in text  # bare & escaped
    # Bold LABELS (our own constants) are still real HTML bold.
    assert "<b>Company</b>:" in text
    assert "<b>Stack</b>:" in text
    # Verdict rationale newlines + emojis survive (escaped body).
    assert "✅ Owns RAG &amp; routing" in text
    assert "&lt;english&gt; rusty" in text


def test_card_section_separation(conn):
    """Header / fields / verdict / link are visually distinct zones."""
    item_id = store.insert_item(
        conn, raw_text="Remote AI Engineer. Python.", source_channel="@jobs",
        source_message_id="sep1", source_link="https://t.me/jobs/3",
    )
    ex = {"title": "AI Engineer", "stack": ["python"], "remote": True,
          "source_channel": "@jobs", "source_link": "https://t.me/jobs/3",
          "Обоснование": "Good fit (80)\n✅ applied LLM"}
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex), relevance_score=80.0)
    item = store.get_item(conn, item_id)
    text = bot.render_surfaced(item)
    # Blank-line separators between zones.
    assert "\n\n" in text
    # A thin separator precedes the verdict zone.
    assert "─────────" in text
    # Order: header first, link last.
    lines = [l for l in text.splitlines() if l]
    assert lines[0].startswith("🟢 80/100")
    assert lines[-1].startswith("<b>🔗 Open original</b>:")


def test_card_renders_verdict_plus_bullets_after_label(conn):
    """The «Обоснование» is now a verdict line + ✅/⚠️ full-sentence bullets; the
    card must render that whole multi-line string verbatim immediately after the
    💭 Обоснование: label (no reformatting, bullets preserved)."""
    reasoning = (
        "Сильный фит по роли — 84/100\n"
        "✅ Роль — applied-LLM, где RAG и роутинг в ядре, что совпадает с её "
        "хендз-он опытом.\n"
        "⚠️ Требуется свободный английский прямо сейчас, что слегка снижает оценку."
    )
    item_id = store.insert_item(
        conn, raw_text="Удалённый AI инженер. RAG, роутинг, промпты.",
        source_channel="@jobs",
        source_message_id="ms1", source_link="https://t.me/jobs/7",
    )
    ex = {"title": "AI инженер", "stack": ["python", "rag"], "remote": True,
          "source_channel": "@jobs", "source_link": "https://t.me/jobs/7",
          "Обоснование": reasoning}
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex), relevance_score=84.0)
    item = store.get_item(conn, item_id)
    text = bot.render_surfaced(item)
    # Label present (bolded) and the full verdict+bullets rationale rendered.
    assert "<b>💭 Обоснование:</b>" in text
    assert reasoning in text
    # The rationale appears immediately after the label line, verbatim.
    assert "<b>💭 Обоснование:</b>\n" + reasoning in text
    # Verdict line carries the score; ✅/⚠️ bullets survive on their own lines.
    assert "Сильный фит по роли — 84/100" in text
    lines = text.splitlines()
    assert any(l.startswith("✅") for l in lines)
    assert any(l.startswith("⚠️") for l in lines)


# --- Buttons: Bot API 9.4 style + emoji fallback + unchanged callback_data ---


def test_surfaced_buttons_have_style_emoji_and_unchanged_callback():
    spec = bot.surfaced_keyboard_spec(7)
    by_action = {bot.decode_callback(cb)[0]: (label, cb, style)
                 for label, cb, style in spec}
    # callback_data UNCHANGED (same encoding handlers/allowlist rely on).
    assert by_action["approve"][1] == bot.encode_callback("approve", 7)
    assert by_action["backlog"][1] == bot.encode_callback("backlog", 7)
    assert by_action["skip"][1] == bot.encode_callback("skip", 7)
    # Bot API 9.4 colour styles.
    assert by_action["approve"][2] == "success"
    assert by_action["skip"][2] == "danger"
    assert by_action["backlog"][2] == "primary"
    # Emoji fallback labels for colour-blind clients.
    assert by_action["approve"][0].startswith("✅")
    assert by_action["skip"][0].startswith("⏭️")
    assert by_action["backlog"][0].startswith("📥")


def test_built_markup_serialises_style_into_json():
    """Passthrough check: the built InlineKeyboardMarkup carries `style` and it
    is serialised into the JSON aiogram sends to the Bot API."""
    kb = bot._build_keyboard(bot.surfaced_keyboard_spec(5))
    buttons = kb.inline_keyboard[0]
    styles = {bot.decode_callback(b.callback_data)[0]: getattr(b, "style", None)
              for b in buttons}
    assert styles == {"approve": "success", "backlog": "primary", "skip": "danger"}
    # callback_data unchanged on the actual button objects.
    assert buttons[0].callback_data == bot.encode_callback("approve", 5)
    # style is present in the serialised JSON sent to Telegram.
    dumped = kb.model_dump_json(exclude_none=True)
    assert '"style":"success"' in dumped
    assert '"style":"danger"' in dumped
    assert '"style":"primary"' in dumped


def test_draft_button_has_style_and_emoji():
    spec = bot.draft_keyboard_spec(9)
    label, cb, style = spec[0][0], spec[0][1], spec[0][2]
    # Manual-confirm: stable "send" action token (T12 DRAFTED->SENT unchanged),
    # only the label/UX changed (no auto-send semantics).
    assert bot.decode_callback(cb)[0] == "send"
    assert cb == bot.encode_callback("send", 9)
    assert style == "success"
    assert label == "✅ Отправила"


# --- Draft keyboard: no auto-send; manual-confirm + optional copy contact ----


def test_draft_keyboard_no_autosend_only_manual_confirm():
    """The draft keyboard must NOT send the application anywhere. The single
    state-affecting button is the manual-confirm the operator taps AFTER they
    sent the отклик by hand. Without a contact there is exactly one button."""
    spec = bot.draft_keyboard_spec(5)
    assert len(spec) == 1
    label, cb, style = spec[0][0], spec[0][1], spec[0][2]
    assert label == "✅ Отправила"
    # callback routes to DECISION_SEND (manual confirm), no auto-send anywhere.
    assert bot.ACTION_TO_DECISION[bot.decode_callback(cb)[0]] == bot.DECISION_SEND


def test_draft_keyboard_copy_contact_present_when_short():
    spec = bot.draft_keyboard_spec(5, "hr@example.com")
    assert len(spec) == 2
    copy_btn = spec[1]
    assert copy_btn[0] == "📋 Контакт"
    # copy_text payload == the contact; copy buttons carry NO callback_data.
    label, data, style, copy_text = copy_btn
    assert data is None
    assert copy_text == "hr@example.com"


def test_draft_keyboard_copy_contact_absent_when_null():
    spec = bot.draft_keyboard_spec(5, None)
    assert len(spec) == 1  # manual-confirm only; no copy button.


def test_draft_keyboard_copy_contact_absent_when_over_256():
    long_contact = "x" * 257  # exceeds the Bot API copy_text cap.
    spec = bot.draft_keyboard_spec(5, long_contact)
    assert len(spec) == 1
    # Exactly 256 is allowed.
    assert len(bot.draft_keyboard_spec(5, "x" * 256)) == 2


def test_draft_copy_text_serialises_into_button_json():
    """copy_text rides the pydantic extra='allow' passthrough (like style) and
    must serialise to the Bot API CopyTextButton shape {"text": ...}."""
    kb = bot._build_keyboard(bot.draft_keyboard_spec(5, "hr@example.com"))
    dumped = kb.model_dump_json(exclude_none=True)
    assert '"copy_text":{"text":"hr@example.com"}' in dumped
    # The manual-confirm button still carries its stable callback_data + style.
    assert '"callback_data":"jh:send:5"' in dumped
    assert '"style":"success"' in dumped


def test_send_decision_final_state_line_is_otpravleno():
    assert bot.final_state_line(bot.DECISION_SEND) == "✅ Отправлено"


# --- Link preview disabled on the card send ---------------------------------


class _CaptureBot:
    """Captures send_message kwargs without any network."""

    def __init__(self):
        self.calls = []
        from aiogram.types import LinkPreviewOptions  # noqa: F401

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append({"chat_id": chat_id, "text": text, **kwargs})


def _surfaced_item_for_send(conn):
    item_id = store.insert_item(
        conn, raw_text="Remote AI Engineer. Python.", source_channel="@jobs",
        source_message_id="snd1", source_link="https://t.me/jobs/4",
    )
    ex = {"title": "AI Engineer", "stack": ["python"], "remote": True,
          "source_channel": "@jobs", "source_link": "https://t.me/jobs/4",
          "Обоснование": "Fit (75)\n✅ ok"}
    # Move to SURFACED so notify_surfaced proceeds.
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex), relevance_score=75.0)
    store.update_state(conn, item_id, SURFACED, from_state="extracted",
                       kind="deterministic", actor="system")
    return item_id


def test_notify_surfaced_disables_link_preview_and_uses_html(conn, deps):
    item_id = _surfaced_item_for_send(conn)
    b = bot.JobHunterBot(_cfg(), conn, deps)
    b._bot = _CaptureBot()  # skip _ensure's real Bot; inject capture
    b._ensure = lambda: None  # type: ignore[assignment]

    asyncio.run(b.notify_surfaced(item_id))

    assert len(b._bot.calls) == 1
    call = b._bot.calls[0]
    # HTML parse mode for the bold labels.
    assert call.get("parse_mode") == "HTML"
    # Link preview disabled.
    lpo = call.get("link_preview_options")
    assert lpo is not None
    assert lpo.is_disabled is True
    # Buttons attached.
    assert call.get("reply_markup") is not None


def test_notify_draft_disables_link_preview(conn, deps):
    item_id = store.insert_item(conn, raw_text="x", source_channel="@c",
                                source_message_id="d1")
    ex = {"title": "AI Eng", "draft": "Apply here: https://t.me/jobs/4"}
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex))
    store.update_state(conn, item_id, DRAFTED, from_state="extracted",
                       kind="deterministic", actor="system")
    b = bot.JobHunterBot(_cfg(), conn, deps)
    b._bot = _CaptureBot()
    b._ensure = lambda: None  # type: ignore[assignment]

    asyncio.run(b.notify_draft(item_id))
    assert len(b._bot.calls) == 1
    lpo = b._bot.calls[0].get("link_preview_options")
    assert lpo is not None and lpo.is_disabled is True


# --- handle_callback with mocked aiogram CallbackQuery ----------------------


class FakeMessage:
    def __init__(self):
        self.markup_cleared = False

    async def edit_reply_markup(self, reply_markup=None):
        self.markup_cleared = reply_markup is None


ALLOWED_UID = 777


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeCallback:
    def __init__(self, data, user_id=ALLOWED_UID):
        self.data = data
        self.answered = None
        self.answer_calls = 0
        self.message = FakeMessage()
        self.from_user = FakeUser(user_id)

    async def answer(self, text=None):
        self.answer_calls += 1
        self.answered = text


class FakeAiogramMessage:
    """Stand-in for an aiogram Message reaching the outer middleware."""

    def __init__(self, user_id):
        self.from_user = FakeUser(user_id)
        # Messages have no .data attribute (unlike callback queries).


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
        source_channel="@c", source_message_id="1",
    )
    pipeline.run_to_gate(conn, item_id, deps=deps)
    assert store.get_item(conn, item_id).state == SURFACED
    return item_id


def test_handle_callback_skip(conn, deps):
    item_id = _surface_item(conn, deps)
    b = bot.JobHunterBot(_cfg(), conn, deps)
    cb = FakeCallback(bot.encode_callback("skip", item_id))
    status = asyncio.run(b.handle_callback(cb))
    assert status == "moved"
    assert store.get_item(conn, item_id).state == SKIPPED
    assert cb.answered == "Skipped"
    assert cb.message.markup_cleared


def test_handle_callback_backlog(conn, deps):
    item_id = _surface_item(conn, deps)
    b = bot.JobHunterBot(_cfg(), conn, deps)
    cb = FakeCallback(bot.encode_callback("backlog", item_id))
    asyncio.run(b.handle_callback(cb))
    assert store.get_item(conn, item_id).state == BACKLOG


def test_handle_callback_approve_drives_to_draft(conn, fake_llm, fake_fx):
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 85, "Обоснование": "ok"}')
    fake_llm.set_for("research", '{"summary":"s","talking_points":[],"questions":[]}')
    fake_llm.set_for("application message", "Hi I want to apply")
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)

    item_id = _surface_item(conn, deps)
    b = bot.JobHunterBot(_cfg(), conn, deps)

    # Stub notify_draft so we don't touch a real Bot.
    sent = {}

    async def fake_notify_draft(iid):
        sent["id"] = iid

    b.notify_draft = fake_notify_draft  # type: ignore[assignment]

    cb = FakeCallback(bot.encode_callback("approve", item_id))
    asyncio.run(b.handle_callback(cb))

    item = store.get_item(conn, item_id)
    assert item.state == DRAFTED
    assert sent["id"] == item_id
    draft_text = json.loads(item.extracted_json)["draft"]
    assert draft_text.startswith("Hi I want to apply")
    # Deterministic signature appended (generic example-profile placeholders).
    assert "github.com/example" in draft_text
    assert "[resume: link]" in draft_text


def test_handle_callback_bad_data(conn, deps):
    b = bot.JobHunterBot(_cfg(), conn, deps)
    cb = FakeCallback("garbage")
    status = asyncio.run(b.handle_callback(cb))
    assert status == "bad_callback"


# --- Access control: pure is_allowed helper --------------------------------


def test_is_allowed_pure():
    allowed = {1, 2, 3}
    assert bot.is_allowed(2, allowed) is True
    assert bot.is_allowed(9, allowed) is False
    # Fails closed on unknown / empty.
    assert bot.is_allowed(None, allowed) is False
    assert bot.is_allowed(1, set()) is False


# --- Access control: handle_callback guard (callback path) ------------------


def test_allowed_user_callback_runs(conn, deps):
    item_id = _surface_item(conn, deps)
    b = bot.JobHunterBot(_cfg(allowed={ALLOWED_UID}), conn, deps)
    cb = FakeCallback(bot.encode_callback("skip", item_id), user_id=ALLOWED_UID)
    status = asyncio.run(b.handle_callback(cb))
    assert status == "moved"
    assert store.get_item(conn, item_id).state == SKIPPED


def test_non_allowed_user_callback_dropped(conn, deps, monkeypatch):
    item_id = _surface_item(conn, deps)
    before = store.get_item(conn, item_id).state
    b = bot.JobHunterBot(_cfg(allowed={ALLOWED_UID}), conn, deps)

    # Spy: business logic must NOT be called.
    called = {"advance": 0}
    monkeypatch.setattr(
        pipeline, "advance_by_id",
        lambda *a, **k: called.__setitem__("advance", called["advance"] + 1),
    )

    cb = FakeCallback(bot.encode_callback("skip", item_id), user_id=999)
    status = asyncio.run(b.handle_callback(cb))

    assert status == "forbidden"
    assert called["advance"] == 0
    # No state change.
    assert store.get_item(conn, item_id).state == before
    assert before == SURFACED
    # Spinner is silenced with an empty ack, but no business ack text.
    assert cb.answer_calls == 1
    assert cb.answered is None
    assert cb.message.markup_cleared is False


# --- Access control: outer-middleware gate (message AND callback paths) -----


def _run_gate(b, event):
    """Invoke the bot's access gate with a spy handler; return whether the
    handler ran."""
    gate = b._make_access_gate()
    ran = {"hit": False}

    async def handler(event, data):
        ran["hit"] = True
        return "ran"

    result = asyncio.run(gate(handler, event, {}))
    return ran["hit"], result


def test_gate_allows_message_for_allowed_user(conn, deps):
    b = bot.JobHunterBot(_cfg(allowed={ALLOWED_UID}), conn, deps)
    msg = FakeAiogramMessage(user_id=ALLOWED_UID)
    ran, result = _run_gate(b, msg)
    assert ran is True
    assert result == "ran"


def test_gate_drops_message_for_non_allowed_user(conn, deps):
    b = bot.JobHunterBot(_cfg(allowed={ALLOWED_UID}), conn, deps)
    msg = FakeAiogramMessage(user_id=999)
    ran, result = _run_gate(b, msg)
    assert ran is False
    assert result is None


def test_gate_allows_callback_for_allowed_user(conn, deps):
    b = bot.JobHunterBot(_cfg(allowed={ALLOWED_UID}), conn, deps)
    cb = FakeCallback(bot.encode_callback("skip", 1), user_id=ALLOWED_UID)
    ran, result = _run_gate(b, cb)
    assert ran is True
    assert result == "ran"


def test_gate_drops_callback_for_non_allowed_user(conn, deps):
    b = bot.JobHunterBot(_cfg(allowed={ALLOWED_UID}), conn, deps)
    cb = FakeCallback(bot.encode_callback("skip", 1), user_id=999)
    ran, result = _run_gate(b, cb)
    assert ran is False
    assert result is None
    # Callback spinner silenced with empty ack; no logic.
    assert cb.answer_calls == 1
    assert cb.answered is None


# --- notify() lifecycle: every send awaited, session closed LAST ------------


class FakeLifecycleBot:
    """A mockable stand-in for JobHunterBot used to verify the notify
    lifecycle WITHOUT any real network / aiogram.

    It models a send as a two-phase awaitable so a NON-awaited coroutine is
    detectable: ``started`` increments synchronously when the coroutine begins,
    and ``completed`` increments only AFTER it has yielded to the loop and
    resumed. If a caller fired-and-forgot a send (never awaited it), completed
    would stay below started.

    Every meaningful action is appended to ``events`` in order so tests can
    assert that all 'send' events precede the 'close' event.
    """

    def __init__(self):
        import asyncio as _asyncio

        self._asyncio = _asyncio
        self.started = 0
        self.completed = 0
        self.sent_ids = []
        self.events = []
        self.closed = False
        self.close_calls = 0

    async def notify_surfaced(self, item_id):
        self.started += 1
        self.events.append(("send_start", item_id))
        # Yield to the loop so a truly-awaited send round-trips; a fire-and-
        # forget coroutine would never reach the lines below.
        await self._asyncio.sleep(0)
        if self.closed:
            # A send must NEVER run after the session is closed.
            raise AssertionError(f"send for {item_id} ran AFTER close")
        self.completed += 1
        self.sent_ids.append(item_id)
        self.events.append(("send", item_id))

    async def aclose(self):
        self.close_calls += 1
        self.closed = True
        self.events.append(("close", None))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()


def test_notify_awaits_every_send():
    fake = FakeLifecycleBot()
    ids = [1, 2, 3, 4, 5]
    sent = asyncio.run(bot.notify(fake, ids))
    # Each surfaced id produced an awaited send: completed == started == N.
    assert fake.started == len(ids)
    assert fake.completed == len(ids)
    assert fake.completed == fake.started == len(ids)
    assert sorted(fake.sent_ids) == ids
    assert sorted(sent) == ids


def test_notify_does_not_close_session():
    """notify() must NOT close the session -- the caller owns teardown so the
    session outlives notify and is closed only after it returns."""
    fake = FakeLifecycleBot()
    asyncio.run(bot.notify(fake, [1, 2]))
    assert fake.closed is False
    assert fake.close_calls == 0


def test_full_lifecycle_sends_then_close_in_order():
    """Model run.py teardown: notify(bot, ids) THEN aclose(). All 'send'
    events must precede the single 'close' event, and close runs last."""
    fake = FakeLifecycleBot()
    ids = [10, 11, 12]

    async def scenario():
        try:
            await bot.notify(fake, ids)
        finally:
            await fake.aclose()

    asyncio.run(scenario())

    # Close happened exactly once, and is the LAST event.
    assert fake.close_calls == 1
    assert fake.events[-1] == ("close", None)
    # Every send completed before close.
    send_indices = [i for i, (k, _) in enumerate(fake.events) if k == "send"]
    close_index = next(i for i, (k, _) in enumerate(fake.events) if k == "close")
    assert len(send_indices) == len(ids)
    assert all(si < close_index for si in send_indices)
    # And every send actually finished (no fire-and-forget).
    assert fake.completed == fake.started == len(ids)


def test_notify_isolates_a_failing_send_but_still_awaits_all():
    """A single failing send is isolated; the others still complete and notify
    waits for every coroutine to settle before returning."""

    class PartialFailBot(FakeLifecycleBot):
        async def notify_surfaced(self, item_id):
            self.started += 1
            self.events.append(("send_start", item_id))
            await self._asyncio.sleep(0)
            if item_id == 2:
                raise RuntimeError("boom")
            self.completed += 1
            self.sent_ids.append(item_id)
            self.events.append(("send", item_id))

    fake = PartialFailBot()
    sent = asyncio.run(bot.notify(fake, [1, 2, 3]))
    # Started all three; two succeeded.
    assert fake.started == 3
    assert sorted(sent) == [1, 3]
    assert sorted(fake.sent_ids) == [1, 3]


def test_notify_empty_is_noop():
    fake = FakeLifecycleBot()
    sent = asyncio.run(bot.notify(fake, []))
    assert sent == []
    assert fake.started == 0


def test_aclose_idempotent_and_safe_without_bot(conn, deps):
    """aclose() is safe when the Bot was never constructed (zero surfaced
    items) and is idempotent (close exactly once)."""
    b = bot.JobHunterBot(_cfg(), conn, deps)
    # _ensure never ran -> no underlying Bot; aclose must be a no-op.
    asyncio.run(b.aclose())
    assert b._closed is False  # nothing to close, flag untouched

    # With a fake underlying bot, aclose closes the session exactly once.
    class FakeSession:
        def __init__(self):
            self.closes = 0

        async def close(self):
            self.closes += 1

    class FakeAioBot:
        def __init__(self):
            self.session = FakeSession()

    b._bot = FakeAioBot()

    async def run_twice():
        await b.aclose()
        await b.aclose()  # second call must be a no-op

    asyncio.run(run_twice())
    assert b._bot.session.closes == 1
    assert b._closed is True


# --- Tester additions (3rd pass) --------------------------------------------


def test_source_link_with_special_chars_is_escaped(conn):
    """source_link containing & and < must be HTML-escaped in Zone 4 and Zone 2.

    The existing escaping test (test_card_escapes_dynamic_values_to_not_break_html)
    uses a safe URL for source_link.  This test confirms source_link itself goes
    through _esc() before insertion into the HTML body so a URL with query-string
    ampersands or angle brackets cannot break the parse_mode=HTML markup.
    """
    item_id = store.insert_item(
        conn, raw_text="Remote AI Engineer. English text.",
        source_channel="@jobs", source_message_id="esc_link1",
        source_link="https://t.me/jobs/3?a=1&b=<x>",
    )
    ex = {
        "title": "AI Eng",
        "stack": ["python"],
        "remote": True,
        "source_channel": "@jobs",
        "source_link": "https://t.me/jobs/3?a=1&b=<x>",
    }
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex), relevance_score=75.0)
    item = store.get_item(conn, item_id)
    text = bot.render_surfaced(item)

    # Raw unescaped ampersand and angle bracket must NOT appear anywhere.
    assert "&b=" not in text, "bare & from source_link appeared unescaped"
    assert "<x>" not in text, "bare <x> from source_link appeared unescaped"
    # Escaped forms must appear.
    assert "&amp;b=" in text
    assert "&lt;x&gt;" in text
    # The bold label must still be valid HTML (not broken by the escaped URL).
    assert "<b>🔗 Open original</b>:" in text


def test_notify_draft_does_not_use_html_parse_mode(conn, deps):
    """notify_draft sends plain text (render_draft output has no HTML markup)
    and therefore must NOT set parse_mode='HTML'.  If it did, any < > & in the
    draft body (LLM-generated text) would be silently swallowed by Telegram."""
    item_id = store.insert_item(conn, raw_text="x", source_channel="@c",
                                source_message_id="d_parse1")
    ex = {"title": "AI Eng", "draft": "Hello <Company> & friends"}
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex))
    store.update_state(conn, item_id, "drafted", from_state="extracted",
                       kind="deterministic", actor="system")

    b = bot.JobHunterBot(_cfg(), conn, deps)
    b._bot = _CaptureBot()
    b._ensure = lambda: None  # type: ignore[assignment]

    asyncio.run(b.notify_draft(item_id))
    assert len(b._bot.calls) == 1
    call = b._bot.calls[0]
    # render_draft is plain text; parse_mode must be absent (None or not set)
    assert call.get("parse_mode") is None, (
        f"notify_draft must NOT set parse_mode; got {call.get('parse_mode')!r}"
    )
