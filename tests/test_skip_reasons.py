"""«Отклонить» at the entry gate: free-text reason + chips (dashboard), skipped state.

The Telegram bot no longer has reason chips or a /reasons pass (the operator works in
the dashboard); its Skip button is now «Отклонить» and simply rejects.
"""
import json

import pytest
from fastapi.testclient import TestClient

from job_hunter import bot, pipeline, skip_reasons, store, webapi
from job_hunter.states import SKIPPED, SURFACED
from tests.conftest import (
    TEST_SUPERUSER_ID, FakeFx, FakeLLM, apply_auth_overrides, make_session_cookie,
)


# --- pure -------------------------------------------------------------------

def test_chip_set_matches_the_agreed_reasons():
    assert list(skip_reasons.CHIPS) == ["stale", "geo", "fit", "seniority", "pay", "other"]
    assert skip_reasons.RELEVANCE_NEGATIVE == {"fit", "seniority"}
    assert skip_reasons.EXCLUDED_FROM_PRECISION == {"stale"}
    assert skip_reasons.GUARD_SIGNAL == {"geo"}


def test_toggle_encode_decode():
    cur = skip_reasons.toggle(skip_reasons.toggle([], "fit"), "stale")
    assert cur == ["stale", "fit"]
    assert skip_reasons.toggle(cur, "stale") == ["fit"]
    assert skip_reasons.toggle(cur, "bogus") == cur
    assert skip_reasons.decode(skip_reasons.encode(["fit", "geo"])) == ["geo", "fit"]
    assert skip_reasons.decode("") == []


def test_frontend_chip_codes_match_the_backend():
    import re, pathlib
    js = (pathlib.Path(__file__).resolve().parent.parent / "frontend" / "src" / "lib.js").read_text(encoding="utf-8")
    block = js[js.index("REJECT_CHIPS"):js.index("];", js.index("REJECT_CHIPS"))]
    assert re.findall(r'code: "(\w+)"', block) == list(skip_reasons.CHIPS)


def test_bot_has_no_reasons_command_or_chip_code():
    import inspect
    src = inspect.getsource(bot)
    assert "handle_reasons" not in src and "reason_keyboard_spec" not in src and 'Command("reasons")' not in src


# --- store ------------------------------------------------------------------

def _surfaced(conn, mid="1", title="Backend dev"):
    iid = store.insert_item(conn, raw_text=f"{title} remote, python. https://acme.io/jobs/{mid}",
                            source_channel="@c", source_message_id=mid)
    for a, b in (("discovered", "extracted"), ("extracted", "scored"), ("scored", "surfaced")):
        store.update_state(conn, iid, b, from_state=a, kind="deterministic", actor="system",
                           extracted_json=json.dumps({"title": title}) if b == "extracted" else None,
                           relevance_score=72.0 if b == "scored" else None)
    return iid


def test_store_roundtrip_keeps_first_source(conn):
    a = _surfaced(conn)
    assert store.get_skip_reasons(conn, a) is None
    store.set_skip_reasons(conn, a, ["fit", "stale"], "dashboard")
    store.set_skip_reasons(conn, a, ["fit"], "other")
    assert store.get_skip_reasons(conn, a) == ["fit"]
    assert conn.execute("select source from skip_reasons where item_id=%s", (a,)).fetchone()["source"] == "dashboard"


def test_reasons_row_does_not_block_deleting_the_item(conn):
    a = _surfaced(conn)
    store.set_skip_reasons(conn, a, ["fit"], "dashboard")
    store.delete_item(conn, a)  # ON DELETE CASCADE
    assert store.get_item(conn, a) is None


# --- dashboard API: POST /api/items/{id}/reject -----------------------------

@pytest.fixture
def client(conn, auth_conn):
    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    app.dependency_overrides[webapi.get_fx] = lambda: FakeFx()
    app.dependency_overrides[webapi.get_deps] = lambda: pipeline.Deps(
        llm_client=FakeLLM(), fx=FakeFx(), use_llm_extract=False)
    apply_auth_overrides(app, auth_conn)
    with TestClient(app) as c:
        c.cookies.set("hl_session", make_session_cookie(TEST_SUPERUSER_ID))
        yield c
    app.dependency_overrides.clear()


BROWSER = {"Origin": "https://dash.example"}


def _transition(conn, iid):
    return conn.execute("select reason, channel, actor_id, kind from state_transitions "
                        "where item_id=%s and to_state='skipped'", (iid,)).fetchone()


def test_reject_with_text_and_chips(conn, client):
    iid = _surfaced(conn)
    r = client.post(f"/api/items/{iid}/reject", headers=BROWSER,
                    json={"reason": "  закрыта, дата месячной давности  ", "chips": ["stale", "fit", "nope"]})
    assert r.status_code == 200 and r.json()["status"] == SKIPPED
    t = _transition(conn, iid)
    assert t["reason"] == "закрыта, дата месячной давности"      # verbatim (trimmed), not 'human:skip'
    assert (t["channel"], t["actor_id"], t["kind"]) == ("dashboard", str(TEST_SUPERUSER_ID), "hitl")
    assert store.get_skip_reasons(conn, iid) == ["stale", "fit"]    # unknown chip dropped


def test_plain_reject_needs_no_body(conn, client):
    iid = _surfaced(conn)
    assert client.post(f"/api/items/{iid}/reject", headers=BROWSER).status_code == 200
    assert _transition(conn, iid)["reason"] == "human:skip"
    assert store.get_skip_reasons(conn, iid) is None                # nothing to save


def test_reject_works_from_backlog_and_conflicts_elsewhere(conn, client):
    iid = _surfaced(conn)
    store.update_state(conn, iid, "backlog", from_state="surfaced", kind="hitl", actor="human")
    assert client.post(f"/api/items/{iid}/reject", headers=BROWSER, json={"chips": ["geo"]}).status_code == 200
    other = _surfaced(conn, "2")
    store.update_state(conn, other, "approved", from_state="surfaced", kind="hitl", actor="human")
    r = client.post(f"/api/items/{other}/reject", headers=BROWSER, json={"chips": ["fit"]})
    assert r.status_code == 409
    assert store.get_skip_reasons(conn, other) is None              # no chips saved on a failed move


def test_reject_unknown_item_and_auth(conn, client, auth_conn):
    assert client.post("/api/items/999999/reject", headers=BROWSER).status_code == 404
    client.cookies.clear()
    assert client.post("/api/items/1/reject").status_code == 401


def test_old_skip_endpoint_still_answers_for_stale_tabs(conn, client):
    iid = _surfaced(conn)
    assert client.post(f"/api/items/{iid}/skip", headers=BROWSER).status_code == 200


# --- bot: Skip button is now «Отклонить» and just rejects -------------------

class _Msg:
    def __init__(self): self.cleared = False
    async def edit_reply_markup(self, reply_markup=None): self.cleared = reply_markup is None
    async def edit_text(self, text, reply_markup=None, parse_mode=None): self.cleared = reply_markup is None


class _Cb:
    def __init__(self, data, uid=777):
        self.data, self.message = data, _Msg()
        self.from_user = type("U", (), {"id": uid})()
    async def answer(self, text=None): self.text = text


def test_bot_reject_button_rejects_and_strips_keyboard(conn, deps):
    import asyncio
    from job_hunter.config import Config
    iid = _surfaced(conn)
    b = bot.JobHunterBot(Config(bot_token="x", notify_chat_id=1, anthropic_api_key=None,
                                allowed_user_ids={777}), conn, deps)
    cb = _Cb(bot.encode_callback("skip", iid))
    assert asyncio.run(b.handle_callback(cb)) == "moved"
    assert store.get_item(conn, iid).state == SKIPPED and cb.message.cleared and cb.text == "Отклонено"
