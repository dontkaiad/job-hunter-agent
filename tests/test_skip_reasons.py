"""Skip-reason chips: live (right after Skip) and the /reasons retro pass."""
import asyncio
import json

import pytest

from job_hunter import bot, pipeline, skip_reasons, store
from job_hunter.config import Config
from job_hunter.pipeline import Deps
from job_hunter.states import SKIPPED, SURFACED

ALLOWED = 777


# --- pure -------------------------------------------------------------------

def test_chip_set_matches_the_agreed_reasons():
    assert list(skip_reasons.CHIPS) == ["stale", "geo", "fit", "seniority", "pay", "other"]
    assert skip_reasons.RELEVANCE_NEGATIVE == {"fit", "seniority"}
    assert skip_reasons.EXCLUDED_FROM_PRECISION == {"stale"}
    assert skip_reasons.GUARD_SIGNAL == {"geo"}


def test_toggle_encode_decode():
    cur = skip_reasons.toggle([], "fit")
    cur = skip_reasons.toggle(cur, "stale")
    assert cur == ["stale", "fit"]                      # display order, several allowed
    assert skip_reasons.toggle(cur, "stale") == ["fit"]  # second tap removes
    assert skip_reasons.toggle(cur, "bogus") == cur       # unknown ignored
    assert skip_reasons.decode(skip_reasons.encode(["fit", "geo"])) == ["geo", "fit"]
    assert skip_reasons.decode("") == []


def test_keyboard_marks_selected_and_roundtrips_callbacks():
    rows = bot.reason_keyboard_spec(42, ["fit"], "live")
    labels = [r[0][0] for r in rows]
    assert labels[2].startswith("✅ ") and not labels[0].startswith("✅")
    actions = [bot.decode_callback(r[0][1]) for r in rows]
    assert actions[0] == ("rsl_stale", 42) and actions[-1] == ("rsl_done", 42)
    assert bot.decode_callback(bot.reason_keyboard_spec(7, [], "retro")[-1][0][1]) == ("rsr_done", 7)
    assert all(len(r[0][1].encode()) <= 64 for r in rows)  # Telegram callback_data limit


# --- fakes ------------------------------------------------------------------

class Msg:
    def __init__(self):
        self.markup = "unset"
        self.text = None

    async def edit_reply_markup(self, reply_markup=None):
        self.markup = reply_markup

    async def edit_text(self, text, reply_markup=None, parse_mode=None, link_preview_options=None):
        self.text, self.markup = text, reply_markup


class Cb:
    def __init__(self, data, uid=ALLOWED):
        self.data, self.message, self.answered = data, Msg(), None
        self.from_user = type("U", (), {"id": uid})()

    async def answer(self, text=None):
        self.answered = text


def _cfg():
    return Config(bot_token="x", notify_chat_id=1, anthropic_api_key=None,
                  allowed_user_ids={ALLOWED})


def _skipped(conn, mid, title="Backend dev"):
    iid = store.insert_item(conn, raw_text=f"{title} remote, python, 3k EUR. @hr", source_channel="@c",
                            source_message_id=str(mid))
    store.update_state(conn, iid, "extracted", from_state="discovered", kind="deterministic",
                       actor="system", extracted_json=json.dumps({"title": title, "company": "Acme",
                                                                  "stack": ["python"]}),
                       relevance_score=72.0)
    for a, b in (("extracted", "scored"), ("scored", "surfaced"), ("surfaced", "skipped")):
        store.update_state(conn, iid, b, from_state=a, kind="hitl" if b == "skipped" else "deterministic",
                           actor="human" if b == "skipped" else "system")
    return iid


@pytest.fixture
def b(conn):
    return bot.JobHunterBot(_cfg(), conn, Deps())


def run(coro):
    return asyncio.run(coro)


# --- store ------------------------------------------------------------------

def test_store_roundtrip_and_queue(conn):
    a, c = _skipped(conn, 1), _skipped(conn, 2)
    assert store.get_skip_reasons(conn, a) is None
    assert store.next_unreviewed_skip(conn).id == a
    assert store.skip_reason_progress(conn) == (0, 2)
    store.set_skip_reasons(conn, a, ["fit", "stale"], "retro")
    store.set_skip_reasons(conn, a, ["fit"], "live")        # update keeps the first source
    assert store.get_skip_reasons(conn, a) == ["fit"]
    assert conn.execute("select source from skip_reasons where item_id=%s", (a,)).fetchone()["source"] == "retro"
    assert store.next_unreviewed_skip(conn).id == c
    assert store.skip_reason_progress(conn) == (1, 2)


def test_reasons_row_does_not_block_deleting_the_item(conn):
    a = _skipped(conn, 1)
    store.set_skip_reasons(conn, a, ["fit"], "live")
    store.delete_item(conn, a)  # ON DELETE CASCADE
    assert store.get_item(conn, a) is None


# --- live: chips right after Skip ------------------------------------------

def test_skip_keeps_the_decision_and_offers_chips(conn, deps, b):
    item_id = store.insert_item(conn, raw_text="Remote Senior Python LLM RAG Claude FastAPI. 250000-350000 RUB. @hr",
                                source_channel="@c", source_message_id="1")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    assert store.get_item(conn, item_id).state == SURFACED
    cb = Cb(bot.encode_callback("skip", item_id))
    assert run(b.handle_callback(cb)) == "moved"
    assert store.get_item(conn, item_id).state == SKIPPED          # decision recorded
    kb = cb.message.markup
    assert kb is not None and len(kb.inline_keyboard) == 7         # 6 chips + Done
    assert store.get_skip_reasons(conn, item_id) is None           # ignoring chips loses nothing


def test_live_toggle_saves_each_tap_and_multiple_chips(conn, b):
    iid = _skipped(conn, 1)
    for code in ("fit", "seniority", "fit"):
        cb = Cb(bot.encode_callback("rsl_" + code, iid))
        assert run(b.handle_callback(cb)) == "toggled"
    assert store.get_skip_reasons(conn, iid) == ["seniority"]
    cb = Cb(bot.encode_callback("rsl_stale", iid))
    run(b.handle_callback(cb))
    assert store.get_skip_reasons(conn, iid) == ["stale", "seniority"]
    marked = [r[0].text for r in cb.message.markup.inline_keyboard]
    assert marked[0].startswith("✅") and marked[3].startswith("✅")


def test_live_done_strips_keyboard_and_saves_nothing_extra(conn, b):
    iid = _skipped(conn, 1)
    cb = Cb(bot.encode_callback("rsl_done", iid))
    assert run(b.handle_callback(cb)) == "done"
    assert cb.message.markup is None and store.get_skip_reasons(conn, iid) is None


def test_chips_refused_for_non_skipped_and_unknown(conn, b, deps):
    item_id = store.insert_item(conn, raw_text="x", source_channel="@c", source_message_id="9")
    cb = Cb(bot.encode_callback("rsl_fit", item_id))
    assert run(b.handle_callback(cb)) == "not_skipped"
    iid = _skipped(conn, 2)
    assert run(b.handle_callback(Cb(bot.encode_callback("rsl_nope", iid)))) == "unknown_reason"


def test_chip_callbacks_are_allowlist_gated(conn, b):
    iid = _skipped(conn, 1)
    cb = Cb(bot.encode_callback("rsl_fit", iid), uid=999)
    assert run(b.handle_callback(cb)) == "forbidden"
    assert store.get_skip_reasons(conn, iid) is None


# --- retro: /reasons --------------------------------------------------------

class Reply:
    def __init__(self):
        self.sent = []

    async def answer(self, text, parse_mode=None, reply_markup=None, link_preview_options=None):
        self.sent.append((text, reply_markup))


def test_reasons_command_walks_the_queue_blind_to_score(conn, b):
    first, second = _skipped(conn, 1, "First role"), _skipped(conn, 2, "Second role")
    r = Reply()
    run(b.handle_reasons(r))
    text, kb = r.sent[0]
    assert "First role" in text and "1/2" in text
    assert "72" not in text and "Обоснование" not in text          # no score, no rationale
    cb = Cb(bot.encode_callback("rsr_geo", first))
    run(b.handle_callback(cb))
    cb = Cb(bot.encode_callback("rsr_done", first))
    assert run(b.handle_callback(cb)) == "next"
    assert store.get_skip_reasons(conn, first) == ["geo"]
    assert "Second role" in cb.message.text and "2/2" in cb.message.text


def test_done_without_chips_marks_reviewed_and_advances(conn, b):
    first, second = _skipped(conn, 1), _skipped(conn, 2)
    run(b.handle_callback(Cb(bot.encode_callback("rsr_done", first))))
    assert store.get_skip_reasons(conn, first) == []
    assert store.next_unreviewed_skip(conn).id == second


def test_finished_queue_says_so(conn, b):
    a = _skipped(conn, 1)
    run(b.handle_callback(Cb(bot.encode_callback("rsr_done", a))))
    cb = Cb(bot.encode_callback("rsr_done", a))
    run(b.handle_callback(cb))
    assert "размечены: 1 из 1" in cb.message.text and cb.message.markup is None
