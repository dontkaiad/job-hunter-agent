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


# --- /borderline READ-ONLY command -----------------------------------------


from job_hunter.states import REJECTED  # noqa: E402


class FakeAnswerMessage:
    """A fake aiogram Message whose .answer captures the reply (no network)."""

    def __init__(self, user_id=ALLOWED_UID):
        self.from_user = FakeUser(user_id)
        self.replies = []

    async def answer(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})


def _seed_borderline(conn, *, score, msg_id, state=REJECTED, company="Acme",
                     title="Backend", link="https://t.me/jobs/1"):
    """Insert an item, set extracted_json + score, move to ``state``."""
    item_id = store.insert_item(
        conn, raw_text="x", source_channel="@c",
        source_message_id=str(msg_id), source_link=link,
    )
    ex = json.dumps({"company": company, "title": title}, ensure_ascii=False)
    store.update_state(conn, item_id, "extracted", from_state="discovered",
                       kind="deterministic", actor="system",
                       extracted_json=ex, relevance_score=score)
    if state != "extracted":
        store.update_state(conn, item_id, state, from_state="extracted",
                           kind="deterministic", actor="system")
    return item_id


def test_render_borderline_pure_compact_list():
    # Multi-line card blocks: header line + indented link line per card. No ⚠️
    # reason line here because these fixtures carry no stored «Обоснование».
    class _It:
        def __init__(self, score, company, title, link):
            self.relevance_score = score
            self.extracted_json = json.dumps({"company": company, "title": title})
            self.source_link = link
    items = [
        _It(59.0, "Acme", "Backend", "https://t.me/a/1"),
        _It(50.0, "Globex", "Frontend", "https://t.me/g/2"),
    ]
    out = bot.render_borderline(items)
    lines = out.splitlines()
    assert lines[0] == "59 — Acme · Backend"
    assert lines[1] == "   https://t.me/a/1"
    assert lines[2] == "50 — Globex · Frontend"
    assert lines[3] == "   https://t.me/g/2"
    # No reason line: the fixtures have no stored «Обоснование».
    assert "⚠️" not in out


def test_render_borderline_two_warning_bullets():
    """A card whose «Обоснование» has a verdict line + a ✅ + TWO ⚠️ bullets:
    the reason line keeps BOTH concerns joined with ' · ', drops the ✅ and the
    verdict, and stays within the ~120-char total cap."""
    reasoning = (
        "Слабоватый фит (52)\n"
        "✅ релевантный стек Python/LLM\n"
        "⚠️ требуется senior-грейд\n"
        "⚠️ обязательный диплom по CS"
    )

    class _It:
        relevance_score = 52.0
        extracted_json = json.dumps(
            {"company": "Bell Integrator", "title": "AI/LLM Инженер",
             "Обоснование": reasoning},
            ensure_ascii=False,
        )
        source_link = "https://t.me/x/1"

    out = bot.render_borderline([_It()])
    lines = out.splitlines()
    assert lines[0] == "52 — Bell Integrator · AI/LLM Инженер"
    reason_line = lines[1]
    assert reason_line.startswith("   ⚠️ ")
    reason = reason_line[len("   ⚠️ "):]
    # Both concerns present, joined with ' · '.
    assert "senior-грейд" in reason
    assert "обязательный диплom" in reason
    assert " · " in reason
    # Verdict line and the ✅ positive are dropped.
    assert "Слабоватый" not in out
    assert "✅" not in out
    assert "релевантный стек" not in out
    # Total reason within the ~120-char cap.
    assert len(reason) <= 120
    assert lines[2] == "   https://t.me/x/1"


def test_render_borderline_no_warning_bullets_omits_reason():
    """A card with only ✅ positives (no ⚠️) emits NO reason line and no crash."""
    class _It:
        relevance_score = 55.0
        extracted_json = json.dumps(
            {"company": "Acme", "title": "Dev",
             "Обоснование": "Хороший фит (55)\n✅ стек совпадает\n✅ удалёнка"},
            ensure_ascii=False,
        )
        source_link = "https://t.me/x/2"

    out = bot.render_borderline([_It()])
    lines = out.splitlines()
    assert lines == ["55 — Acme · Dev", "   https://t.me/x/2"]
    assert "⚠️" not in out
    assert bot._borderline_reason(_It()) == ""


def test_render_borderline_missing_or_garbage_reasoning_no_crash():
    """Missing / garbage / empty «Обоснование» -> no reason line, no crash."""
    class _Missing:
        relevance_score = 53.0
        extracted_json = json.dumps({"company": "C", "title": "T"})
        source_link = None

    class _Garbage:
        relevance_score = 53.0
        extracted_json = "{not valid json"
        source_link = None

    class _Empty:
        relevance_score = 53.0
        extracted_json = json.dumps({"company": "C", "title": "T",
                                     "Обоснование": ""}, ensure_ascii=False)
        source_link = None

    for it in (_Missing(), _Garbage(), _Empty()):
        assert bot._borderline_reason(it) == ""
        out = bot.render_borderline([it])
        assert "⚠️" not in out
        assert "None" not in out


def test_render_borderline_long_bullets_trimmed():
    """Very long ⚠️ bullets: per-bullet ~60-char trim AND the ~120-char total
    cap are both enforced (assert the '…' truncation)."""
    long_a = "A" * 200
    long_b = "B" * 200
    reasoning = f"Verdict (51)\n⚠️ {long_a}\n⚠️ {long_b}"

    class _It:
        relevance_score = 51.0
        extracted_json = json.dumps(
            {"company": "Co", "title": "Role", "Обоснование": reasoning},
            ensure_ascii=False,
        )
        source_link = None

    reason = bot._borderline_reason(_It())
    # Total cap enforced with ellipsis truncation.
    assert len(reason) <= 120
    assert reason.endswith("…")
    # Per-bullet trim is applied before the join (no single 200-char run).
    assert "A" * 61 not in reason


def test_render_borderline_inline_bullets_parsed():
    """Inline (non-newline-separated) ⚠️ bullets are still parsed — the split is
    on the markers, not on newlines."""
    reasoning = "Маргинально (54) ✅ есть опыт ⚠️ нет визы ⚠️ только офис"

    class _It:
        relevance_score = 54.0
        extracted_json = json.dumps(
            {"company": "Co", "title": "Role", "Обоснование": reasoning},
            ensure_ascii=False,
        )
        source_link = None

    reason = bot._borderline_reason(_It())
    assert "нет визы" in reason
    assert "только офис" in reason
    assert " · " in reason
    # The ✅ positive and verdict are dropped even when inline.
    assert "есть опыт" not in reason
    assert "Маргинально" not in reason


def test_render_borderline_empty_one_liner():
    assert bot.render_borderline([]) == "нет пограничных вакансий (50-59)"
    assert bot.render_borderline([]) == bot.BORDERLINE_EMPTY


def test_handle_borderline_lists_band_only(conn, deps):
    # In band [50,60): 50, 55, 59.  Out of band: 49, 60, 80, NULL.
    _seed_borderline(conn, score=49.0, msg_id=1, company="Low", title="LowRole")
    _seed_borderline(conn, score=50.0, msg_id=2, company="Fifty", title="R50")
    _seed_borderline(conn, score=55.0, msg_id=3, company="FiftyFive", title="R55")
    _seed_borderline(conn, score=59.0, msg_id=4, company="FiftyNine", title="R59")
    _seed_borderline(conn, score=60.0, msg_id=5, company="Sixty", title="R60",
                     state=SURFACED)
    _seed_borderline(conn, score=80.0, msg_id=6, company="High", title="RHigh",
                     state=SURFACED)
    # NULL-score discovered item.
    store.insert_item(conn, raw_text="x", source_channel="@c", source_message_id="7")

    b = bot.JobHunterBot(_cfg(), conn, deps)
    msg = FakeAnswerMessage()
    asyncio.run(b.handle_borderline(msg))

    assert len(msg.replies) == 1
    text = msg.replies[0]["text"]
    # Header lines only (multi-line card blocks: header + indented link line;
    # no ⚠️ reason line since these fixtures carry no stored «Обоснование»).
    headers = [ln for ln in text.splitlines() if not ln.startswith("   ")]
    # Exactly the three band items, highest-first.
    assert len(headers) == 3
    assert headers[0] == "59 — FiftyNine · R59"
    assert headers[1] == "55 — FiftyFive · R55"
    assert headers[2] == "50 — Fifty · R50"
    # Out-of-band companies never appear.
    for bad in ("Low", "Sixty", "High"):
        assert bad not in text
    # Link preview disabled, like the other sends.
    lpo = msg.replies[0].get("link_preview_options")
    assert lpo is not None and lpo.is_disabled is True


def test_handle_borderline_empty_band_one_liner(conn, deps):
    # Only out-of-band items.
    _seed_borderline(conn, score=49.0, msg_id=1)
    _seed_borderline(conn, score=70.0, msg_id=2, state=SURFACED)
    b = bot.JobHunterBot(_cfg(), conn, deps)
    msg = FakeAnswerMessage()
    asyncio.run(b.handle_borderline(msg))
    assert msg.replies[0]["text"] == "нет пограничных вакансий (50-59)"


def test_handle_borderline_is_read_only(conn, deps, monkeypatch):
    # Seed a mix; snapshot ALL states before, assert unchanged after.
    ids = [
        _seed_borderline(conn, score=55.0, msg_id=1, state=REJECTED),
        _seed_borderline(conn, score=58.0, msg_id=2, state=REJECTED),
        _seed_borderline(conn, score=80.0, msg_id=3, state=SURFACED),
    ]
    before = {i: store.get_item(conn, i).state for i in ids}

    # advance / update_state must NOT be called by the handler.
    calls = {"advance": 0, "advance_by_id": 0, "update_state": 0, "run_to_gate": 0}
    monkeypatch.setattr(pipeline, "advance_by_id",
                        lambda *a, **k: calls.__setitem__("advance_by_id", 1))
    monkeypatch.setattr(pipeline, "run_to_gate",
                        lambda *a, **k: calls.__setitem__("run_to_gate", 1))
    monkeypatch.setattr(store, "update_state",
                        lambda *a, **k: calls.__setitem__("update_state", 1))

    b = bot.JobHunterBot(_cfg(), conn, deps)
    msg = FakeAnswerMessage()
    asyncio.run(b.handle_borderline(msg))

    after = {i: store.get_item(conn, i).state for i in ids}
    assert after == before
    assert calls == {"advance": 0, "advance_by_id": 0, "update_state": 0, "run_to_gate": 0}


def test_borderline_command_gated_by_middleware(conn, deps):
    # The /borderline message handler is a MESSAGE handler -> covered by the
    # existing access_gate outer middleware. A non-allowlisted sender is dropped
    # before the handler runs (handler never invoked => no reply, no write).
    b = bot.JobHunterBot(_cfg(allowed={ALLOWED_UID}), conn, deps)
    msg = FakeAiogramMessage(user_id=999)
    ran, result = _run_gate(b, msg)
    assert ran is False and result is None
    # And an allowlisted sender passes the gate.
    ok_msg = FakeAiogramMessage(user_id=ALLOWED_UID)
    ran_ok, _ = _run_gate(b, ok_msg)
    assert ran_ok is True


# ---------------------------------------------------------------------------
# Tester-added: Point 6 — Variation selector (U+FE0F) explicit probe
# ---------------------------------------------------------------------------

def test_borderline_reason_variation_selector_full_and_bare():
    """Spec point 6: ⚠️ (U+26A0 U+FE0F) and bare ⚠ (U+26A0 only) must BOTH be
    recognised as ⚠️ markers; no stray U+FE0F must leak into the rendered text;
    ✅ (U+2705) must be dropped.

    The regex is (⚠️?|✅) where the ? makes U+FE0F optional, so the FULL ⚠️ is
    captured entirely as the marker (segment does NOT start with U+FE0F).
    """
    WARNING_FULL = "⚠️"   # U+26A0 + U+FE0F
    WARNING_BARE = "⚠"   # U+26A0 only, no variation selector
    CHECKMARK = "✅"            # U+2705

    reasoning = (
        f"Вердикт (52)\n"
        f"{WARNING_FULL} full-selector concern text\n"
        f"{WARNING_BARE} bare-selector concern text\n"
        f"{CHECKMARK} positive text dropped"
    )

    class _It:
        relevance_score = 52.0
        extracted_json = json.dumps(
            {"company": "Co", "title": "T", "Обоснование": reasoning},
            ensure_ascii=False,
        )
        source_link = None

    reason = bot._borderline_reason(_It())

    # Both ⚠️ concerns must appear in the result.
    assert "full-selector concern text" in reason, (
        "Full ⚠️ (U+26A0+FE0F) concern not extracted"
    )
    assert "bare-selector concern text" in reason, (
        "Bare ⚠ (U+26A0 only) concern not extracted"
    )
    # ✅ positive must be dropped.
    assert "positive text dropped" not in reason, (
        "✅ positive must be dropped from reason"
    )
    # Verdict line must be dropped.
    assert "Вердикт" not in reason, "Verdict line must be dropped"
    # No stray U+FE0F variation selector in the final result.
    U_FE0F = "️"
    assert U_FE0F not in reason, (
        f"Stray U+FE0F found in reason {reason!r}"
    )
    # Joined with ' · '.
    assert " · " in reason


def test_borderline_reason_variation_selector_no_fe0f_leak_in_rendered_card():
    """Spec point 6: when ⚠️ is present, the rendered card line must NOT contain
    a stray U+FE0F byte (i.e. '️' variation selector) in the reason segment."""
    WARNING_FULL = "⚠️"
    reasoning = f"Score (55)\n{WARNING_FULL} concern with full selector"

    class _It:
        relevance_score = 55.0
        extracted_json = json.dumps(
            {"company": "Co", "title": "T", "Обоснование": reasoning},
            ensure_ascii=False,
        )
        source_link = None

    out = bot.render_borderline([_It()])
    # The rendered '   ⚠️ {reason}' line: the ⚠️ in the prefix is fine.
    # But the REASON TEXT segment must not contain a stray selector.
    reason_line = [l for l in out.splitlines() if l.startswith("   ⚠️ ")]
    assert len(reason_line) == 1, f"Expected exactly one reason line; got: {out.splitlines()}"
    # Strip the known '   ⚠️ ' prefix (which legitimately contains FE0F as part
    # of the ⚠️ emoji), then check the remainder.
    prefix = "   ⚠️ "
    reason_segment = reason_line[0][len(prefix):]
    U_FE0F = "️"
    assert U_FE0F not in reason_segment, (
        f"Stray U+FE0F in rendered reason segment: {reason_segment!r}"
    )


# ---------------------------------------------------------------------------
# Tester-added: Point 4 extension — non-str Обоснование (int, list)
# ---------------------------------------------------------------------------

def test_borderline_reason_non_str_obosnovaniye_returns_empty():
    """Spec point 4: when «Обоснование» is present but not a string (int, list,
    dict) _borderline_reason must return '' without crashing."""
    U_FE0F = "️"

    for bad_value in (42, ["✅ good", "⚠️ bad"], {"score": 52}, True, 3.14):
        class _It:
            relevance_score = 52.0
            extracted_json = json.dumps(
                {"company": "C", "title": "T", "Обоснование": bad_value},
                ensure_ascii=False,
            )
            source_link = None

        result = bot._borderline_reason(_It())
        assert result == "", (
            f"Non-str Обоснование {bad_value!r} must yield '' from _borderline_reason; "
            f"got {result!r}"
        )
        rendered = bot.render_borderline([_It()])
        assert "⚠️" not in rendered, (
            f"Non-str Обоснование {bad_value!r} must not produce a reason line"
        )


# ---------------------------------------------------------------------------
# Tester-added: Point 5 exact measurement — per-bullet len <= _BORDERLINE_BULLET_MAX
# ---------------------------------------------------------------------------

def test_borderline_reason_per_bullet_trim_exact_measurement():
    """Spec point 5: each ⚠️ bullet is individually word-trimmed to
    _BORDERLINE_BULLET_MAX (60) chars BEFORE joining. Two SHORT bullets that
    BOTH fit within _BORDERLINE_REASON_MAX (120) are both shown, each <= 60.

    NOTE (revised contract): the OLD behaviour mid-bullet-truncated to pack the
    line; the new STUB-AVOIDANCE rule only joins WHOLE bullets that fit. So this
    test uses short bullets whose join stays <= 120 (both survive); the
    can't-both-fit case is covered by
    test_borderline_reason_two_full_bullets_only_first_kept below.
    """
    # Short distinct concerns; each well under 60, join under 120.
    bullet_a = "нет рабочей визы и разрешения на работу в стране"   # < 60
    bullet_b = "только офис, без удалённой работы вообще"             # < 60
    reasoning = f"Verdict (58)\n⚠️ {bullet_a}\n⚠️ {bullet_b}"

    class _It:
        relevance_score = 58.0
        extracted_json = json.dumps(
            {"company": "Co", "title": "T", "Обоснование": reasoning},
            ensure_ascii=False,
        )
        source_link = None

    reason = bot._borderline_reason(_It())

    # Total cap holds.
    assert len(reason) <= bot._BORDERLINE_REASON_MAX, (
        f"Total reason length {len(reason)} exceeds {bot._BORDERLINE_REASON_MAX}"
    )
    # Both short concerns are visible — neither ate the other's budget.
    assert bullet_a in reason
    assert bullet_b in reason
    parts = reason.split(" · ")
    assert len(parts) == 2, f"Expected two whole bullets joined by ' · '; got {parts!r}"
    for p in parts:
        assert len(p) <= bot._BORDERLINE_BULLET_MAX, (
            f"Bullet {p!r} ({len(p)}) exceeds per-bullet limit "
            f"{bot._BORDERLINE_BULLET_MAX}"
        )


def test_borderline_reason_two_full_bullets_only_first_kept():
    """Revised contract (stub-avoidance): two ~60-char whole bullets cannot both
    fit (60+3+60 > 120). Show ONLY the first whole bullet, never a fragment of
    the second, and the first stays <= _BORDERLINE_BULLET_MAX."""
    # Each oversized -> hard-cut packs ~59 chars; 59+3+59=121 > 120.
    bullet_a = "CONCERN_A" + "a" * 200
    bullet_b = "CONCERN_B" + "b" * 200
    reasoning = f"Verdict (58)\n⚠️ {bullet_a}\n⚠️ {bullet_b}"

    class _It:
        relevance_score = 58.0
        extracted_json = json.dumps(
            {"company": "Co", "title": "T", "Обоснование": reasoning},
            ensure_ascii=False,
        )
        source_link = None

    reason = bot._borderline_reason(_It())
    assert len(reason) <= bot._BORDERLINE_REASON_MAX
    assert len(reason) <= bot._BORDERLINE_BULLET_MAX  # single whole bullet
    assert " · " not in reason, f"second bullet must not be fragmented in: {reason!r}"
    assert reason.startswith("CONCERN_A")
    assert "CONCERN_B" not in reason
    assert reason.endswith("…")


# ---------------------------------------------------------------------------
# Tester-added: Point 7 — score TRUNCATION (not rounding): 59.x -> "59"
# ---------------------------------------------------------------------------

def test_render_borderline_score_truncation_not_rounding():
    """Spec point 7: score must be TRUNCATED via int() — 59.9 must render as
    '59' not '60'. This is critical: '60' would look like a threshold-cleared card.
    Also: 50.0 -> '50', 52.7 -> '52', None -> '?'.
    """
    cases = [
        (59.9, "59"),
        (59.1, "59"),
        (52.7, "52"),
        (50.0, "50"),
        (None, "?"),
    ]
    for score, expected_str in cases:
        class _It:
            relevance_score = score
            extracted_json = json.dumps({"company": "Co", "title": "T"})
            source_link = None

        out = bot.render_borderline([_It()])
        first_line = out.splitlines()[0]
        score_token = first_line.split(" ")[0]
        assert score_token == expected_str, (
            f"Score {score!r} must render as {expected_str!r}, got {score_token!r} "
            f"(full line: {first_line!r})"
        )


# ---------------------------------------------------------------------------
# Tester-added: Point 8 — overflow N correctness
# ---------------------------------------------------------------------------

def test_render_borderline_overflow_n_is_correct():
    """Spec point 8: when the budget overflows, the '…и ещё N' line must report
    the EXACT count of items NOT rendered (total - rendered_count).

    Build items large enough to force overflow within 100 items, then verify
    the reported N equals total_items - rendered_items.
    """
    long_link = "https://t.me/" + "x" * 60

    class _LargeItem:
        def __init__(self, n):
            self.relevance_score = 50.0 + (n % 10)
            self.extracted_json = json.dumps({
                "company": "C" * 50,
                "title": "T" * 50,
                "Обоснование": f"V (5{n%10})\n⚠️ concern for item {n} with extra padding text here",
            }, ensure_ascii=False)
            self.source_link = long_link + f"/{n}"

    items = [_LargeItem(n) for n in range(100)]
    result = bot.render_borderline(items)

    assert len(result) <= 4096, f"Output length {len(result)} exceeds Telegram limit"
    assert "…и ещё" in result, "Expected overflow marker '…и ещё' in result"

    lines = result.splitlines()
    # Count rendered header lines (non-indented, non-overflow).
    rendered = [l for l in lines if l and not l.startswith("   ") and "…и ещё" not in l]
    overflow_lines = [l for l in lines if "…и ещё" in l]
    assert len(overflow_lines) == 1, f"Expected exactly one overflow line; got {overflow_lines}"

    import re as _re
    m = _re.search(r"…и ещё (\d+)", overflow_lines[0])
    assert m is not None, f"Could not parse N from overflow line: {overflow_lines[0]!r}"
    n_reported = int(m.group(1))
    n_expected = len(items) - len(rendered)
    assert n_reported == n_expected, (
        f"Overflow N={n_reported} but expected {n_expected} "
        f"({len(items)} total - {len(rendered)} rendered)"
    )


# ---------------------------------------------------------------------------
# WORD-BOUNDARY _soft_trim + stub-avoidance _borderline_reason
# ---------------------------------------------------------------------------

def _seg(it):
    """Build a single-item borderline card and return its reason segment text
    (the part after the '   ⚠️ ' prefix), or None when no reason line emitted."""
    out = bot.render_borderline([it])
    prefix = "   ⚠️ "
    for ln in out.splitlines():
        if ln.startswith(prefix):
            return ln[len(prefix):]
    return None


def test_soft_trim_cuts_on_word_boundary_not_mid_word():
    """A long multi-word string is cut at a whole-word boundary: the body before
    '…' is a prefix of the input ending exactly at a space (no partial word)."""
    text = "требует 3–4 года опыта в разработке распределённых систем"
    limit = 28
    out = bot._soft_trim(text, limit)
    assert len(out) <= limit
    assert out.endswith("…")
    body = out[:-1]
    # The body must equal the input truncated at a space boundary: i.e. the input
    # continues with a space right where the body ends (whole-word cut).
    assert text.startswith(body), f"body {body!r} not a prefix of input"
    assert text[len(body):len(body) + 1] == " ", (
        f"body must end at a word boundary (next input char is a space); got {out!r}"
    )
    # Concretely: it must NOT cut mid-word into 'разрабо…'.
    assert "разрабо…" not in out
    assert out == "требует 3–4 года опыта в…"


def test_soft_trim_strips_trailing_opening_quote_before_ellipsis():
    """A cut landing just after an opening guillemet drops the '«' before '…':
    '…без оговорки «желательно»' -> '…без оговорки…' not '…без оговорки «…'."""
    text = "кандидат без оговорки «желательно» по визе и релокации"
    # Choose a limit so the word-budget boundary falls right after '«'.
    limit = 24
    out = bot._soft_trim(text, limit)
    assert len(out) <= limit
    assert out.endswith("…")
    assert "«" not in out, f"opening guillemet must be stripped before '…'; got {out!r}"
    assert "«…" not in out
    assert out == "кандидат без оговорки…"


def test_soft_trim_strips_trailing_comma_or_dash_before_ellipsis():
    """A trailing comma/dash at the cut point is stripped before '…'."""
    out_comma = bot._soft_trim("первое слово, второе слово третье", 20)
    assert out_comma.endswith("…") and "," not in out_comma
    assert out_comma == "первое слово…"
    out_dash = bot._soft_trim("первое слово — второе слово третье", 16)
    assert out_dash.endswith("…")
    # Neither the dash nor a trailing space remains before the ellipsis.
    assert not out_dash[:-1].endswith(("—", " ", "–", "-"))


def test_soft_trim_single_token_longer_than_limit_hard_cut_fallback():
    """A single token with no space in the budget hard-cuts to limit-1 + '…',
    still <= limit."""
    text = "Х" * 200  # one unbroken token, no spaces
    limit = 60
    out = bot._soft_trim(text, limit)
    assert len(out) == limit
    assert len(out) <= limit
    assert out.endswith("…")
    assert out == "Х" * (limit - 1) + "…"


def test_soft_trim_noop_when_within_limit():
    """No '…' and no change when text already fits."""
    assert bot._soft_trim("короткий текст", 60) == "короткий текст"


def test_borderline_reason_stub_avoidance_two_bullets_only_first_shown():
    """Two ~60-char bullets cannot both fit cleanly (60+3+60=123 > 120): show
    ONLY the first whole bullet, with no second fragment."""
    # Each word-trims to ~59 chars; 59+3+59=121 > 120 -> only the first fits.
    a = "Роль заявлена как senior уровень и требует обязательно 3–4 полных года опыта в разработке"
    b = "Обязателен профильный диплом и подтверждённый коммерческий опыт в крупном финтехе банке"

    class _It:
        relevance_score = 53.0
        extracted_json = json.dumps(
            {"company": "Co", "title": "T", "Обоснование": f"V (53)\n⚠️ {a}\n⚠️ {b}"},
            ensure_ascii=False,
        )
        source_link = None

    reason = bot._borderline_reason(_It())
    assert len(reason) <= bot._BORDERLINE_REASON_MAX
    # Only ONE bullet -> no join separator present, no fragment of the second.
    assert " · " not in reason, f"expected a single whole bullet, got {reason!r}"
    assert reason.startswith("Роль заявлена как senior")
    assert "Обязателен" not in reason, "second bullet must not be fragmented in"
    # Ends on a whole word, never mid-word.
    assert reason.endswith("…")
    assert reason == "Роль заявлена как senior уровень и требует обязательно 3–4…"


def test_borderline_reason_several_short_bullets_all_shown_joined():
    """Several SHORT whole bullets that all fit are shown joined by ' · '."""
    class _It:
        relevance_score = 55.0
        extracted_json = json.dumps(
            {"company": "Co", "title": "T",
             "Обоснование": "V (55)\n⚠️ нет визы\n⚠️ только офис\n⚠️低 зарплата"},
            ensure_ascii=False,
        )
        source_link = None

    reason = bot._borderline_reason(_It())
    assert len(reason) <= bot._BORDERLINE_REASON_MAX
    assert reason == "нет визы · только офис · 低 зарплата"
    assert reason.count(" · ") == 2


def test_borderline_reason_first_bullet_alone_too_long_word_trimmed_to_cap():
    """A first bullet that alone exceeds the reason cap is word-trimmed down to
    the cap (whole word, ends '…', <= cap)."""
    long_first = (
        "Роль заявлена как senior и требует не менее трёх или четырёх лет "
        "практического опыта в проектировании и разработке распределённых "
        "отказоустойчивых высоконагруженных систем на нескольких языках"
    )

    class _It:
        relevance_score = 51.0
        extracted_json = json.dumps(
            {"company": "Co", "title": "T", "Обоснование": f"V (51)\n⚠️ {long_first}"},
            ensure_ascii=False,
        )
        source_link = None

    reason = bot._borderline_reason(_It())
    assert len(reason) <= bot._BORDERLINE_REASON_MAX
    assert reason.endswith("…")
    # Word-boundary trim: body is a prefix of the (bullet-trimmed) source ending
    # at a space, never mid-word.
    body = reason[:-1]
    assert long_first.startswith(body)
    assert long_first[len(body):len(body) + 1] == " "
    # Meaningful content well above the 15-char floor.
    assert len(reason.rstrip("…")) >= bot._BORDERLINE_MIN_BULLET_CHARS


def test_borderline_reason_min_bullet_floor_no_tiny_stub():
    """Degenerate first bullet that would word-trim to a sub-15-char stub must
    NOT be emitted as a tiny 'сло…'; the floor forces a fuller hard-cut."""
    # First token is short, then one huge unbroken token: word-trim at the first
    # space would yield just the short token. With a >cap whole bullet this only
    # triggers the first-bullet branch when the bullet exceeds the reason cap.
    huge = "ы" * 300
    bullet = f"но {huge}"  # 'но' + space + 300-char token

    class _It:
        relevance_score = 52.0
        extracted_json = json.dumps(
            {"company": "Co", "title": "T", "Обоснование": f"V (52)\n⚠️ {bullet}"},
            ensure_ascii=False,
        )
        source_link = None

    reason = bot._borderline_reason(_It())
    assert len(reason) <= bot._BORDERLINE_REASON_MAX
    assert reason.endswith("…")
    # Must NOT be the tiny 'но…' stub; the >=15-char floor forces a fuller cut.
    assert reason != "но…"
    assert len(reason.rstrip("…")) >= bot._BORDERLINE_MIN_BULLET_CHARS


def test_borderline_reason_rendered_sample_no_midword_and_quote_fixed():
    """End-to-end render: the reason segment ends on a whole word (no 'разрабо…')
    and any opening guillemet at a cut is stripped (no '«…')."""
    a = "Роль заявлена как senior и требует 3–4 года опыта в разработке систем"

    class _It:
        relevance_score = 54.0
        extracted_json = json.dumps(
            {"company": "Bell", "title": "Engineer", "Обоснование": f"V (54)\n⚠️ {a}"},
            ensure_ascii=False,
        )
        source_link = "https://t.me/x/1"

    seg = _seg(_It())
    assert seg is not None
    assert seg.endswith("…")
    assert "разрабо…" not in seg
    assert "«" not in seg


# ---------------------------------------------------------------------------
# Tester-added (2nd pass): adversarial edge cases for _soft_trim and
# _borderline_reason that the existing suite does NOT cover.
# ---------------------------------------------------------------------------

def test_soft_trim_double_ellipsis_bug_u2026_in_input():
    """BUG (Developer must fix): when input already contains U+2026 ('…') at
    a word boundary and _soft_trim cuts there, the body ends with '…' and
    appending another '…' produces '……' (double ellipsis). The result must
    never contain two consecutive ellipsis characters.

    Root cause: U+2026 (HORIZONTAL ELLIPSIS) is not in _TRIM_TRAILING, so
    rstrip() does not remove it from the body before appending '…'.
    Fix: add chr(0x2026) to _TRIM_TRAILING in bot.py.
    """
    text = "слово с многоточием… тут продолжение"
    limit = 22  # budget lands right after '…', before 'тут'
    out = bot._soft_trim(text, limit)
    assert len(out) <= limit
    assert "……" not in out, (
        f"Double ellipsis '……' produced when input contains U+2026 near boundary: "
        f"{out!r}. Fix: add chr(0x2026) to _TRIM_TRAILING."
    )


def test_soft_trim_right_curly_quote_stripped_before_ellipsis():
    """A word ending with a right curly double-quote '”' (") is stripped
    from the tail before '…' is appended, so the result never shows '"…'."""
    # The word '"needed"' has a closing curly quote. If the trim cuts after it,
    # the trailing '"' must be removed.
    text = 'текст с закрытой кавычкой” ещё слова тут дальше'
    limit = 28  # lands just after the closing quote
    out = bot._soft_trim(text, limit)
    assert len(out) <= limit
    assert out.endswith("…")
    assert out[:-1].endswith("”") is False, (
        f"Trailing right curly quote must be stripped before '…'; got {out!r}"
    )


def test_soft_trim_opening_bracket_stripped_before_ellipsis():
    """An opening parenthesis '(' at the word boundary is stripped before '…'."""
    text = "текст (какой-то скобочный блок здесь"
    # The space is right before '(', so rfind puts body right after the previous word.
    # body then may end with '(' after rstrip. Verify no '(' remains.
    limit = 14
    out = bot._soft_trim(text, limit)
    assert len(out) <= limit
    assert out.endswith("…")
    assert "(…" not in out, (
        f"Opening bracket must be stripped before '…'; got {out!r}"
    )
    assert not out[:-1].endswith("("), (
        f"Body must not end with '(' before ellipsis; got {out!r}"
    )


def test_soft_trim_word_exactly_fills_budget_no_spurious_ellipsis():
    """When a word exactly fills limit-1 chars (the full budget), the hard-cut
    path returns that word + '…', NOT an extra '…'. No double ellipsis."""
    # 'abcde' is 5 chars; with limit=6, budget=5 chars='abcde', no space ->
    # hard-cut: 'abcde…' (6 chars). Result must be exactly 'abcde…'.
    out = bot._soft_trim("abcde fghij", 6)
    assert out == "abcde…", f"Expected 'abcde…', got {out!r}"
    assert len(out) == 6
    assert "……" not in out


def test_soft_trim_word_exactly_at_rfind_boundary_inclusive():
    """Off-by-one: a word whose last char is at position limit-2 (so the word
    fits in budget[:cut] exactly). The word is kept whole; no '…' appended
    when len(text) <= limit (direct return), and correct '…' when > limit."""
    # 'helo' (4 chars) at limit=6: text='helo world' (10 chars), limit=6
    # budget='helo w'[:5]='helo w'? No: budget=text[:limit-1]=text[:5]='helo '
    # rfind(' ')=4, body=text[:4]='helo', rstrip->'helo', return 'helo…' (5 <=6)
    out = bot._soft_trim("helo world", 6)
    assert out == "helo…"
    assert len(out) <= 6

    # And when text fits exactly: len('helo w')=6 == limit=6 -> returned as-is
    out_fit = bot._soft_trim("helo w", 6)
    assert out_fit == "helo w"
    assert "…" not in out_fit


def test_soft_trim_consecutive_spaces_in_input_handled():
    """Consecutive spaces in input do NOT cause issues: rfind(' ') still finds
    the last space in the budget and body.rstrip cleans any trailing space."""
    text = "слово   много   пробелов   тут"
    # This text is fed through ' '.join(seg.split()) in _borderline_reason,
    # so _soft_trim itself receives space-normalised text. But test it directly.
    limit = 15
    out = bot._soft_trim(text, limit)
    assert len(out) <= limit
    assert out.endswith("…") or len(text) <= limit
    # Body must not end with trailing spaces
    if out.endswith("…"):
        assert not out[:-1].endswith(" "), f"Trailing space before '…': {out!r}"


def test_borderline_reason_empty_first_bullet_second_is_shown():
    """When the FIRST ⚠️ bullet is all whitespace (condensed to ''), it is
    SKIPPED; the SECOND non-empty bullet is shown as the reason. No crash,
    no empty reason when real concerns exist further in the list."""
    class _It:
        relevance_score = 52.0
        extracted_json = __import__("json").dumps(
            {"company": "C", "title": "T",
             "Обоснование": "V (52)\n⚠️    \n⚠️ реальный повод для отказа"},
            ensure_ascii=False,
        )
        source_link = None

    reason = bot._borderline_reason(_It())
    assert reason == "реальный повод для отказа", (
        f"Empty first bullet must be skipped; second bullet must appear; got {reason!r}"
    )


def test_borderline_reason_consecutive_spaces_collapsed():
    """Consecutive spaces in a ⚠️ bullet segment are collapsed by
    ' '.join(seg.split()), so the rendered reason has single spaces only."""
    class _It:
        relevance_score = 52.0
        extracted_json = __import__("json").dumps(
            {"company": "C", "title": "T",
             "Обоснование": "V (52)\n⚠️ слово   много   пробелов   тут"},
            ensure_ascii=False,
        )
        source_link = None

    reason = bot._borderline_reason(_It())
    assert "  " not in reason, (
        f"Consecutive spaces must be collapsed; got {reason!r}"
    )
    assert reason == "слово много пробелов тут"


def test_soft_trim_hyphenated_token_no_mid_word():
    """A hyphenated token like 'work-life-balance' must not be split mid-hyphen;
    the trim cuts at the nearest SPACE boundary outside the token."""
    text = "требует опыта work-life-balance подтверждённый стаж"
    # 'work-life-balance' has no space inside; rfind(' ') will skip over it.
    for limit in range(20, 45):
        out = bot._soft_trim(text, limit)
        if out.endswith("…"):
            body = out[:-1]
            next_char = text[len(body) : len(body) + 1]
            assert next_char in (" ", ""), (
                f"Mid-word cut at limit={limit}: body={body!r}, next_char={next_char!r}"
            )


def test_soft_trim_length_guarantee_at_bullet_and_reason_caps():
    """Exhaustive length check: for all interesting limits (60 and 120) and
    a range of Cyrillic + mixed inputs, result length is always <= limit."""
    import random
    rng = random.Random(42)
    cyrillic = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    for _ in range(500):
        word_count = rng.randint(1, 15)
        words = []
        for _ in range(word_count):
            length = rng.randint(2, 15)
            words.append("".join(rng.choices(cyrillic, k=length)))
        text = " ".join(words)
        for limit in [bot._BORDERLINE_BULLET_MAX, bot._BORDERLINE_REASON_MAX]:
            out = bot._soft_trim(text, limit)
            assert len(out) <= limit, (
                f"LENGTH VIOLATION: _soft_trim({text!r}, {limit}) -> "
                f"len={len(out)} > {limit}"
            )


def test_soft_trim_en_dash_token_is_not_split():
    """En-dash inside a token ('3–4') is NOT a space; it must not be treated as
    a word boundary. The word-cut path (space found in budget) must produce a
    body ending exactly at a space in the original input.

    The hard-cut fallback (no space in budget) may cut mid-character by design;
    this test only asserts on the word-cut path where rfind returns > 0.
    """
    text = "требует 3–4 года опыта в разработке плюс лидерские"
    for limit in range(5, 55):
        budget = text[: limit - 1]
        cut = budget.rfind(" ")
        if cut <= 0:
            # Hard-cut fallback path: mid-char allowed; skip this limit.
            continue
        out = bot._soft_trim(text, limit)
        if out.endswith("…"):
            body = out[:-1]
            next_char = text[len(body) : len(body) + 1]
            assert next_char in (" ", ""), (
                f"Mid-word at limit={limit}: body={body!r} next={next_char!r}"
            )


def test_borderline_reason_bullet_only_period_is_skipped():
    """A ⚠️ bullet containing ONLY a period (condensed to '') is skipped;
    the next non-empty bullet is used."""
    class _It:
        relevance_score = 52.0
        extracted_json = __import__("json").dumps(
            {"company": "C", "title": "T",
             "Обоснование": "V (52)\n⚠️ .\n⚠️ реальная причина"},
            ensure_ascii=False,
        )
        source_link = None

    reason = bot._borderline_reason(_It())
    assert reason == "реальная причина", (
        f"Period-only bullet must be skipped; got {reason!r}"
    )
