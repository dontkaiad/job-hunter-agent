"""kai-mcp read-only gate: GET /api/mcp/* behind a Bearer MCP_READ_TOKEN, nothing else opens.

The token opens exactly two GETs. It must NOT open any cookie-gated /api/* route (reads or writes),
a missing/short/empty configured token disables the gate, and a session cookie alone is not enough here.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from job_hunter import store, webapi
from job_hunter.config import load_config
from job_hunter.states import SURFACED
from tests.conftest import TEST_SUPERUSER_ID, FakeFx, apply_auth_overrides, make_session_cookie

TOKEN = "t" * 40            # >= MCP_MIN_TOKEN_LEN
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _seed(conn, mid="mcp1"):
    blob = json.dumps({"title": "Backend Engineer", "source_channel": "ch", "company": "Acme", "stack": ["python"],
                       "salary_min": 200000, "salary_max": 300000, "currency": "RUB", "remote": True,
                       "reasons": ["good fit"], "benefits": []}, ensure_ascii=False)
    item_id = store.insert_item(conn, raw_text="raw", source_channel="ch", source_link="http://t.me/x/1",
                                source_message_id=mid)
    store.set_extracted(conn, item_id, blob)
    store.update_state(conn, item_id, SURFACED, from_state="discovered", kind="deterministic", actor="system",
                       reason="seed", extracted_json=blob, relevance_score=85.0)
    return item_id


def _app(conn, auth_conn, token=TOKEN):
    app = webapi.create_app()
    cfg = load_config({"MCP_READ_TOKEN": token} if token is not None else {})
    app.dependency_overrides[webapi.get_config] = lambda: cfg
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    app.dependency_overrides[webapi.get_fx] = lambda: FakeFx()
    apply_auth_overrides(app, auth_conn)
    return app


def test_config_reads_and_strips_token():
    assert load_config({}).mcp_read_token == ""
    assert load_config({"MCP_READ_TOKEN": f"  {TOKEN}  "}).mcp_read_token == TOKEN


def test_no_header_401(conn, auth_conn):
    app = _app(conn, auth_conn)
    with TestClient(app) as c:
        for path in ("/api/mcp/pipeline?status=surfaced", "/api/mcp/pipeline-status"):
            r = c.get(path)
            assert r.status_code == 401, path
            assert r.headers.get("www-authenticate") == "Bearer"
    app.dependency_overrides.clear()


def test_wrong_token_and_wrong_scheme_401(conn, auth_conn):
    app = _app(conn, auth_conn)
    with TestClient(app) as c:
        for headers in ({"Authorization": "Bearer " + "x" * 40},
                        {"Authorization": "Bearer " + TOKEN[:-1]},
                        {"Authorization": "Basic " + TOKEN},
                        {"Authorization": TOKEN},
                        {"Authorization": "Bearer"}):
            assert c.get("/api/mcp/pipeline", headers=headers).status_code == 401, headers
    app.dependency_overrides.clear()


def test_valid_token_reads_pipeline_and_status(conn, auth_conn):
    _seed(conn)
    app = _app(conn, auth_conn)
    with TestClient(app) as c:
        r = c.get("/api/mcp/pipeline?status=surfaced", headers=AUTH)
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list) and len(r.json()) >= 1
        r2 = c.get("/api/mcp/pipeline-status", headers=AUTH)
        assert r2.status_code == 200 and "paused" in r2.json()
    app.dependency_overrides.clear()


def test_valid_token_does_not_open_writes(conn, auth_conn):
    item_id = _seed(conn, mid="mcp_w")
    app = _app(conn, auth_conn)
    with TestClient(app) as c:
        assert c.post(f"/api/items/{item_id}/approve", headers=AUTH).status_code == 401
        assert c.post("/api/pipeline-status", headers=AUTH, json={"paused": True}).status_code == 401
        # no write verbs exist under /api/mcp at all
        assert c.post("/api/mcp/pipeline", headers=AUTH).status_code in (404, 405)
    assert store.get_item(conn, item_id).state == SURFACED       # nothing moved
    app.dependency_overrides.clear()


def test_valid_token_does_not_open_regular_api_reads(conn, auth_conn):
    item_id = _seed(conn, mid="mcp_r")
    app = _app(conn, auth_conn)
    with TestClient(app) as c:
        assert c.get("/api/pipeline", headers=AUTH).status_code == 401
        assert c.get("/api/pipeline-status", headers=AUTH).status_code == 401
        assert c.get(f"/api/items/{item_id}", headers=AUTH).status_code == 401
    app.dependency_overrides.clear()


def test_session_cookie_alone_does_not_open_mcp(conn, auth_conn):
    app = _app(conn, auth_conn)
    with TestClient(app) as c:
        c.cookies.set("hl_session", make_session_cookie(TEST_SUPERUSER_ID))
        assert c.get("/api/pipeline").status_code == 200              # the dashboard itself still works
        assert c.get("/api/mcp/pipeline").status_code == 401           # but the cookie is not a Bearer token
    app.dependency_overrides.clear()


def test_gate_disabled_when_token_unset_short_or_empty(conn, auth_conn):
    for configured in (None, "", "short", "s" * 31):
        app = _app(conn, auth_conn, token=configured)
        with TestClient(app) as c:
            for header in (AUTH, {"Authorization": f"Bearer {configured or ''}"}, {}):
                assert c.get("/api/mcp/pipeline", headers=header).status_code == 401, (configured, header)
        app.dependency_overrides.clear()
