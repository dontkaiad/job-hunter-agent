"""Provenance of state transitions: channel (dashboard|bot|api|script|system) + actor id.

Added after 90 skips of unknown origin could not be attributed (2026-11-01): the
transition row said only actor=human. Forward-only; older rows keep NULL.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from job_hunter import bot, pipeline, store, webapi
from job_hunter.config import Config
from job_hunter.states import APPROVED, SURFACED
from tests.conftest import (
    TEST_SUPERUSER_ID, FakeFx, FakeLLM, apply_auth_overrides, make_session_cookie,
)


def _row(conn, item_id, to_state):
    return conn.execute(
        "SELECT channel, actor_id, kind, actor FROM state_transitions "
        "WHERE item_id=%s AND to_state=%s ORDER BY id DESC LIMIT 1", (item_id, to_state),
    ).fetchone()


def _surfaced(conn):
    iid = store.insert_item(conn, raw_text="Python LLM engineer, remote. Apply https://acme.io/jobs/7",
                            source_channel="@c", source_message_id="1")
    for a, b in (("discovered", "extracted"), ("extracted", "scored"), ("scored", "surfaced")):
        store.update_state(conn, iid, b, from_state=a, kind="deterministic", actor="system",
                           extracted_json=json.dumps({"title": "t"}) if b == "extracted" else None,
                           relevance_score=72.0 if b == "scored" else None)
    return iid


# --- store ------------------------------------------------------------------

def test_default_channels(conn):
    iid = _surfaced(conn)
    assert _row(conn, iid, "surfaced")["channel"] == "system"          # automatic move
    store.update_state(conn, iid, "approved", from_state="surfaced", kind="hitl", actor="human")
    r = _row(conn, iid, "approved")
    assert r["channel"] == "script" and r["actor_id"] is None          # human, origin not stated


def test_explicit_channel_and_actor_are_stored_as_text(conn):
    iid = _surfaced(conn)
    store.update_state(conn, iid, "approved", from_state="surfaced", kind="hitl", actor="human",
                       channel="dashboard", actor_id=424242)
    r = _row(conn, iid, "approved")
    assert (r["channel"], r["actor_id"]) == ("dashboard", "424242")


def test_ingest_and_failure_rows_are_system(conn):
    iid = store.insert_item(conn, raw_text="x", source_channel="@c", source_message_id="9")
    assert _row(conn, iid, "discovered")["channel"] == "system"
    store.record_score_failure(conn, iid, max_attempts=3, reason="score deferred (parse): X")
    last = conn.execute("SELECT channel FROM state_transitions WHERE item_id=%s ORDER BY id DESC LIMIT 1",
                        (iid,)).fetchone()
    assert last["channel"] == "system"


def test_old_table_gets_the_columns_and_old_rows_stay_null(pg_dsn):
    c = store.connect(pg_dsn)
    store.init_db(c)
    iid = store.insert_item(c, raw_text="x", source_channel="@c", source_message_id="1")
    c.execute("ALTER TABLE state_transitions DROP COLUMN channel")
    c.execute("ALTER TABLE state_transitions DROP COLUMN actor_id")
    c.commit()
    store.init_db(c)                                                    # re-adds them
    c.execute("UPDATE state_transitions SET channel = NULL")             # a pre-existing row
    c.commit()
    cols = {r["column_name"] for r in c.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='state_transitions'").fetchall()}
    assert {"channel", "actor_id"} <= cols
    assert c.execute("SELECT channel FROM state_transitions WHERE item_id=%s", (iid,)).fetchone()["channel"] is None
    c.close()


# --- pipeline ---------------------------------------------------------------

def test_advance_passes_provenance_to_the_transition(conn, fake_fx):
    iid = _surfaced(conn)
    pipeline.advance_by_id(conn, iid, decision="approve", deps=pipeline.Deps(fx=fake_fx),
                           channel="api", actor_id="55")
    r = _row(conn, iid, "approved")
    assert (r["channel"], r["actor_id"], r["kind"], r["actor"]) == ("api", "55", "hitl", "human")


# --- bot --------------------------------------------------------------------

class _Msg:
    async def edit_reply_markup(self, reply_markup=None): pass
    async def edit_text(self, *a, **k): pass


class _Cb:
    def __init__(self, data, uid):
        self.data, self.message = data, _Msg()
        self.from_user = type("U", (), {"id": uid})()
    async def answer(self, text=None): pass


def test_bot_button_records_bot_channel_and_user_id(conn, deps):
    iid = _surfaced(conn)
    b = bot.JobHunterBot(Config(bot_token="x", notify_chat_id=1, anthropic_api_key=None,
                                allowed_user_ids={777}), conn, deps)
    asyncio.run(b.handle_callback(_Cb(bot.encode_callback("skip", iid), 777)))
    r = _row(conn, iid, "skipped")
    assert (r["channel"], r["actor_id"]) == ("bot", "777")


# --- dashboard API ----------------------------------------------------------

@pytest.fixture
def client(conn, auth_conn):
    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    app.dependency_overrides[webapi.get_fx] = lambda: FakeFx()
    app.dependency_overrides[webapi.get_deps] = lambda: pipeline.Deps(llm_client=FakeLLM(), fx=FakeFx(),
                                                                      use_llm_extract=False)
    apply_auth_overrides(app, auth_conn)
    with TestClient(app) as c:
        c.cookies.set("hl_session", make_session_cookie(TEST_SUPERUSER_ID))
        yield c
    app.dependency_overrides.clear()


def test_browser_request_is_dashboard_and_bare_client_is_api(conn, client):
    a, b = _surfaced(conn), None
    r = client.post(f"/api/items/{a}/approve", headers={"Origin": "https://dash.example"})
    assert r.status_code == 200
    row = _row(conn, a, "approved")
    assert (row["channel"], row["actor_id"]) == ("dashboard", str(TEST_SUPERUSER_ID))
    c2 = store.insert_item(conn, raw_text="Python LLM engineer, remote.", source_channel="@c", source_message_id="2")
    for x, y in (("discovered", "extracted"), ("extracted", "scored"), ("scored", "surfaced")):
        store.update_state(conn, c2, y, from_state=x, kind="deterministic", actor="system")
    assert client.post(f"/api/items/{c2}/skip").status_code == 200       # no Origin / Referer
    row = _row(conn, c2, "skipped")
    assert (row["channel"], row["actor_id"]) == ("api", str(TEST_SUPERUSER_ID))


def test_decline_reason_still_stored_with_provenance(conn, client):
    iid = _surfaced(conn)
    store.update_state(conn, iid, "approved", from_state="surfaced", kind="hitl", actor="human")
    r = client.post(f"/api/items/{iid}/decline", json={"reason": "вакансия закрыта"},
                    headers={"Referer": "https://dash.example/"})
    assert r.status_code == 200
    row = conn.execute("SELECT reason, channel FROM state_transitions WHERE item_id=%s AND to_state='declined'",
                       (iid,)).fetchone()
    assert (row["reason"], row["channel"]) == ("вакансия закрыта", "dashboard")


def test_unauthenticated_write_is_still_rejected(conn, auth_conn):
    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    apply_auth_overrides(app, auth_conn)
    with TestClient(app) as c:
        assert c.post("/api/items/1/skip").status_code == 401
