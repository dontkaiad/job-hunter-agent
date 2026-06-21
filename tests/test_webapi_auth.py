"""Integration tests for the gated dashboard API + login flow (issue #5).

Verifies the FastAPI glue around tg_auth: every /api route requires a valid
session AND an approved grant (or superuser); the public /login, /auth/callback
and /logout routes behave; and the cross-subdomain cookie attributes are set.

The auth deps are overridden (test secret, test superuser set, the ephemeral
auth-DB connection with seeded grants). get_conn/get_fx/get_deps are overridden
as in the #3/#4 tests so no network/real pipeline DB is touched beyond the
ephemeral PG.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from job_hunter import store, tg_auth, webapi
from job_hunter.pipeline import Deps
from job_hunter.states import SURFACED
from tests.conftest import (
    TEST_COOKIE_DOMAIN,
    TEST_LOGIN_BOT_TOKEN,
    TEST_SESSION_SECRET,
    TEST_SUPERUSER_ID,
    FakeFx,
    FakeLLM,
    apply_auth_overrides,
    make_session_cookie,
)

# Endpoints to sweep: the two GET reads and the 5 POST actions.
GET_ROUTES = ["/api/pipeline", "/api/items/{id}"]
POST_ACTIONS = ["approve", "skip", "backlog", "sent", "draft"]


def _extracted(**over) -> str:
    base = {
        "title": "Backend Engineer",
        "source_channel": "ch",
        "company": "Acme",
        "stack": ["python"],
        "salary_min": 200000,
        "salary_max": 300000,
        "currency": "RUB",
        "remote": True,
        "reasons": ["good fit"],
        "benefits": [],
    }
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


_CTR = [5000]


def _seed(conn, *, state=SURFACED, score=85.0):
    _CTR[0] += 1
    blob = _extracted()
    item_id = store.insert_item(
        conn,
        raw_text="raw",
        source_channel="ch",
        source_link="http://t.me/x/1",
        source_message_id=str(_CTR[0]),
    )
    store.set_extracted(conn, item_id, blob)
    if state != "discovered":
        store.update_state(
            conn, item_id, state, from_state="discovered", kind="deterministic",
            actor="system", reason="seed", extracted_json=blob, relevance_score=score,
        )
    return item_id


def _draft_deps() -> Deps:
    fake = FakeLLM()
    fake.set_for("research", '{"summary":"s","talking_points":[],"questions":[]}')
    fake.set_for("application message", "Hello, I'd love to apply.")
    return Deps(llm_client=fake, fx=FakeFx(), use_llm_extract=False)


def _make_app(conn, auth_conn, **settings_over):
    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    app.dependency_overrides[webapi.get_fx] = lambda: FakeFx()
    app.dependency_overrides[webapi.get_deps] = _draft_deps
    apply_auth_overrides(app, auth_conn, **settings_over)
    return app


def _sweep_all(client, item_id):
    """Hit all 7 gated routes; return list of (label, status_code)."""
    out = []
    out.append(("GET pipeline", client.get("/api/pipeline").status_code))
    out.append(("GET item", client.get(f"/api/items/{item_id}").status_code))
    for action in POST_ACTIONS:
        out.append((f"POST {action}", client.post(f"/api/items/{item_id}/{action}").status_code))
    return out


# --- no cookie -> 401 on ALL routes -----------------------------------------


def test_no_session_401_all_routes(conn, auth_conn):
    item_id = _seed(conn)
    app = _make_app(conn, auth_conn)
    with TestClient(app) as c:  # no cookie
        for label, code in _sweep_all(c, item_id):
            assert code == 401, f"{label} expected 401, got {code}"
    app.dependency_overrides.clear()


# --- valid session + approved grant -> works --------------------------------


def test_approved_grant_allows_reads_and_actions(conn, auth_conn):
    # Non-superuser id with an approved grant.
    tg_auth.upsert_grant(auth_conn, 555, "jobhunter", "approved")
    item_id = _seed(conn, state=SURFACED)
    app = _make_app(conn, auth_conn, superuser_ids=set())
    with TestClient(app) as c:
        c.cookies.set("hl_session", make_session_cookie(555))
        assert c.get("/api/pipeline").status_code == 200
        assert c.get(f"/api/items/{item_id}").status_code == 200
        # approve action works (SURFACED -> APPROVED)
        r = c.post(f"/api/items/{item_id}/approve")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"
    app.dependency_overrides.clear()


def test_superuser_allows_without_grant(conn, auth_conn):
    item_id = _seed(conn, state=SURFACED)
    app = _make_app(conn, auth_conn)  # superuser_ids defaults to {TEST_SUPERUSER_ID}
    with TestClient(app) as c:
        c.cookies.set("hl_session", make_session_cookie(TEST_SUPERUSER_ID))
        assert c.get("/api/pipeline").status_code == 200
        r = c.post(f"/api/items/{item_id}/skip")
        assert r.status_code == 200 and r.json()["status"] == "skipped"
    app.dependency_overrides.clear()


# --- valid session but pending/denied/missing grant -> 403 on all routes ----


@pytest.mark.parametrize("status", ["pending", "denied", None])
def test_unauthorized_grant_403_all_routes(conn, auth_conn, status):
    tg_id = 777
    if status is not None:
        tg_auth.upsert_grant(auth_conn, tg_id, "jobhunter", status)
    item_id = _seed(conn)
    app = _make_app(conn, auth_conn, superuser_ids=set())
    with TestClient(app) as c:
        c.cookies.set("hl_session", make_session_cookie(tg_id))
        for label, code in _sweep_all(c, item_id):
            assert code == 403, f"{label} expected 403 ({status}), got {code}"
    app.dependency_overrides.clear()


# --- tampered / expired cookie -> 401 ---------------------------------------


def test_tampered_cookie_401(conn, auth_conn):
    item_id = _seed(conn)
    app = _make_app(conn, auth_conn)
    good = make_session_cookie(TEST_SUPERUSER_ID)
    tampered = good[:-3] + ("aaa" if not good.endswith("aaa") else "bbb")
    with TestClient(app) as c:
        c.cookies.set("hl_session", tampered)
        assert c.get("/api/pipeline").status_code == 401
    app.dependency_overrides.clear()


def test_wrong_secret_cookie_401(conn, auth_conn):
    item_id = _seed(conn)
    app = _make_app(conn, auth_conn)
    foreign = make_session_cookie(TEST_SUPERUSER_ID, secret="some-other-secret")
    with TestClient(app) as c:
        c.cookies.set("hl_session", foreign)
        assert c.get("/api/pipeline").status_code == 401
    app.dependency_overrides.clear()


# --- /auth/callback: cookie Domain + attributes -----------------------------


def _signed_widget(now: int, tg_id: int) -> dict:
    data = {
        "id": str(tg_id),
        "first_name": "Kai",
        "username": "kai",
        "auth_date": str(now),
    }
    dcs = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret_key = hashlib.sha256(TEST_LOGIN_BOT_TOKEN.encode()).digest()
    data["hash"] = hmac.new(secret_key, dcs.encode(), hashlib.sha256).hexdigest()
    return data


def test_callback_sets_sso_cookie_remember(conn, auth_conn):
    app = _make_app(conn, auth_conn)  # TEST_SUPERUSER_ID is superuser
    now = int(time.time())
    params = _signed_widget(now, TEST_SUPERUSER_ID)
    params["remember"] = "1"
    with TestClient(app) as c:
        r = c.get("/auth/callback", params=params, follow_redirects=False)
        assert r.status_code == 303
        set_cookie = r.headers["set-cookie"]
        assert "hl_session=" in set_cookie
        assert "Domain=.heylark.dev" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=lax" in set_cookie
        # remember=1 -> Max-Age present (~30d)
        assert "Max-Age=2592000" in set_cookie
    app.dependency_overrides.clear()


def test_callback_session_cookie_no_max_age_when_not_remember(conn, auth_conn):
    app = _make_app(conn, auth_conn)
    now = int(time.time())
    params = _signed_widget(now, TEST_SUPERUSER_ID)
    params["remember"] = "0"
    with TestClient(app) as c:
        r = c.get("/auth/callback", params=params, follow_redirects=False)
        assert r.status_code == 303
        set_cookie = r.headers["set-cookie"]
        assert "Domain=.heylark.dev" in set_cookie
        # session cookie -> no Max-Age / no Expires
        assert "Max-Age" not in set_cookie
        assert "expires" not in set_cookie.lower()
    app.dependency_overrides.clear()


def test_callback_invalid_signature_401(conn, auth_conn):
    app = _make_app(conn, auth_conn)
    now = int(time.time())
    params = _signed_widget(now, TEST_SUPERUSER_ID)
    params["hash"] = "deadbeef"  # bad
    with TestClient(app) as c:
        r = c.get("/auth/callback", params=params, follow_redirects=False)
        assert r.status_code == 401
    app.dependency_overrides.clear()


def test_callback_unauthorized_grant_403(conn, auth_conn):
    # Valid signature, non-superuser, no approved grant -> 403.
    app = _make_app(conn, auth_conn, superuser_ids=set())
    now = int(time.time())
    params = _signed_widget(now, 888)
    with TestClient(app) as c:
        r = c.get("/auth/callback", params=params, follow_redirects=False)
        assert r.status_code == 403
    app.dependency_overrides.clear()


def test_callback_then_authenticated_request(conn, auth_conn):
    # Full flow: callback issues a cookie whose token reads back to the tg_id and
    # is accepted by the gate. (We re-attach the token explicitly because the
    # cross-subdomain Domain=.heylark.dev cookie is not sent back to the
    # TestClient's "testserver" host — that's correct browser behaviour.)
    item_id = _seed(conn)
    app = _make_app(conn, auth_conn)
    now = int(time.time())
    params = _signed_widget(now, TEST_SUPERUSER_ID)
    params["remember"] = "1"
    with TestClient(app) as c:
        r = c.get("/auth/callback", params=params, follow_redirects=False)
        assert r.status_code == 303
        # Extract the issued session token from the raw Set-Cookie header (httpx
        # won't store the cross-domain cookie for "testserver") and verify it.
        set_cookie = r.headers["set-cookie"]
        token = set_cookie.split("hl_session=", 1)[1].split(";", 1)[0]
        assert token
        assert tg_auth.read_session(token, secret=TEST_SESSION_SECRET) == TEST_SUPERUSER_ID
        morsel = token
        # Re-attach explicitly and confirm the gate now lets the request through.
        c.cookies.set("hl_session", morsel)
        assert c.get("/api/pipeline").status_code == 200
    app.dependency_overrides.clear()


# --- /logout clears the cookie with the same Domain -------------------------


def test_logout_clears_cookie_same_domain(conn, auth_conn):
    app = _make_app(conn, auth_conn)
    with TestClient(app) as c:
        r = c.post("/logout")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        set_cookie = r.headers["set-cookie"]
        assert "hl_session=" in set_cookie
        assert "Domain=.heylark.dev" in set_cookie
        # A clearing cookie is empty + expired.
        assert ('Max-Age=0' in set_cookie) or ('expires=' in set_cookie.lower())
    app.dependency_overrides.clear()


# --- /login HTML page -------------------------------------------------------


def test_login_page_html(conn, auth_conn):
    app = _make_app(conn, auth_conn)
    with TestClient(app) as c:
        r = c.get("/login")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        body = r.text
        # Widget script + the login bot username present.
        assert "telegram-widget.js" in body
        assert "l4rk_test_bot" in body
    app.dependency_overrides.clear()


def test_login_page_is_public_no_auth(conn, auth_conn):
    # /login must not require a session.
    app = _make_app(conn, auth_conn)
    with TestClient(app) as c:  # no cookie
        assert c.get("/login").status_code == 200
    app.dependency_overrides.clear()
