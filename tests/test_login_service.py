"""Tests for the standalone login service (login.heylark.dev).

Covers:
  - is_safe_next: allowed / rejected cases (open-redirect guard)
  - GET /: page renders, includes bot username + next param in callback URL
  - GET /auth/callback: valid/invalid widget, remember-me, safe/unsafe next
"""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from fastapi.testclient import TestClient

from job_hunter.login_service import LoginSettings, create_app, is_safe_next

TEST_BOT_TOKEN = "999999:test-login-bot-token"
TEST_SECRET = "test-login-secret"
TEST_BOT_USERNAME = "l4rk_test_bot"
TEST_COOKIE_DOMAIN = ".heylark.dev"
TEST_PUBLIC_URL = "https://login.heylark.dev"


def _make_settings(**over) -> LoginSettings:
    base = dict(
        session_secret=TEST_SECRET,
        login_bot_token=TEST_BOT_TOKEN,
        login_bot_username=TEST_BOT_USERNAME,
        cookie_domain=TEST_COOKIE_DOMAIN,
        public_url=TEST_PUBLIC_URL,
    )
    base.update(over)
    return LoginSettings(**base)


def _make_client(**settings_over) -> TestClient:
    from job_hunter.login_service import get_login_settings

    _app = create_app()
    settings = _make_settings(**settings_over)
    _app.dependency_overrides[get_login_settings] = lambda: settings
    return TestClient(_app)


def _sign_widget(tg_id: int, now: int, *, token: str = TEST_BOT_TOKEN) -> dict:
    data = {
        "id": str(tg_id),
        "first_name": "Kai",
        "username": "kai",
        "auth_date": str(now),
    }
    dcs = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret_key = hashlib.sha256(token.encode()).digest()
    data["hash"] = hmac.new(secret_key, dcs.encode(), hashlib.sha256).hexdigest()
    return data


# --- is_safe_next: allowed --------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://heylark.dev",
        "https://heylark.dev/dashboard",
        "https://heylark.dev/some/path?q=1",
        "https://cats.heylark.dev",
        "https://jobs.heylark.dev",
        "https://jobs.heylark.dev/some/path?q=1",
        "https://login.heylark.dev",
    ],
)
def test_is_safe_next_allows_heylark(url):
    assert is_safe_next(url) is True


# --- is_safe_next: rejected -------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "",
        None,
        "http://heylark.dev",
        "http://jobs.heylark.dev",
        "https://evil.com",
        "https://heylark.dev.evil.com",   # suffix attack
        "https://notHeylark.dev",
        "//evil.com",                      # proto-relative
        "//heylark.dev",                   # proto-relative even for heylark
        "/relative",
        "relative",
        "javascript:alert(1)",
        "ftp://heylark.dev",
        "https://evil.com/https://heylark.dev",
    ],
)
def test_is_safe_next_rejects_unsafe(url):
    assert is_safe_next(url) is False


def test_is_safe_next_rejects_backslash():
    # Backslash-based bypass e.g. https://heylark.dev\.evil.com
    assert is_safe_next("https://heylark.dev\\.evil.com") is False
    assert is_safe_next("\\heylark.dev") is False


# --- GET / ------------------------------------------------------------------


def test_login_page_renders_widget():
    with _make_client() as c:
        r = c.get("/")
    assert r.status_code == 200
    assert "telegram-widget.js" in r.text
    assert TEST_BOT_USERNAME in r.text


def test_login_page_absolute_callback_url():
    with _make_client(public_url="https://login.heylark.dev") as c:
        r = c.get("/")
    assert "https://login.heylark.dev/auth/callback" in r.text


def test_login_page_relative_callback_url_when_no_public_url():
    with _make_client(public_url="") as c:
        r = c.get("/")
    assert 'data-auth-url="/auth/callback"' in r.text


def test_login_page_next_embedded_in_callback_url():
    with _make_client() as c:
        r = c.get("/", params={"next": "https://jobs.heylark.dev"})
    assert r.status_code == 200
    # next must appear URL-encoded inside the callback_url
    assert "next=" in r.text
    assert "jobs.heylark.dev" in r.text


# --- GET /auth/callback -----------------------------------------------------


def test_callback_invalid_signature_401():
    now = int(time.time())
    params = _sign_widget(42, now)
    params["hash"] = "deadbeef"
    with _make_client() as c:
        r = c.get("/auth/callback", params=params, follow_redirects=False)
    assert r.status_code == 401


def test_callback_sets_sso_cookie_attributes():
    now = int(time.time())
    params = _sign_widget(42, now)
    with _make_client() as c:
        r = c.get("/auth/callback", params=params, follow_redirects=False)
    assert r.status_code == 303
    sc = r.headers["set-cookie"]
    assert "hl_session=" in sc
    assert "Domain=.heylark.dev" in sc
    assert "HttpOnly" in sc
    assert "Secure" in sc
    assert "SameSite=lax" in sc


def test_callback_remember_sets_max_age():
    now = int(time.time())
    params = _sign_widget(42, now)
    params["remember"] = "1"
    with _make_client() as c:
        r = c.get("/auth/callback", params=params, follow_redirects=False)
    assert "Max-Age=2592000" in r.headers["set-cookie"]


def test_callback_no_remember_session_cookie():
    now = int(time.time())
    params = _sign_widget(42, now)
    params["remember"] = "0"
    with _make_client() as c:
        r = c.get("/auth/callback", params=params, follow_redirects=False)
    sc = r.headers["set-cookie"]
    assert "Max-Age" not in sc
    assert "expires" not in sc.lower()


def test_callback_safe_next_redirects_there():
    now = int(time.time())
    params = _sign_widget(42, now)
    params["next"] = "https://jobs.heylark.dev"
    with _make_client() as c:
        r = c.get("/auth/callback", params=params, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://jobs.heylark.dev"


def test_callback_unsafe_next_redirects_to_default():
    now = int(time.time())
    params = _sign_widget(42, now)
    params["next"] = "https://evil.com"
    with _make_client() as c:
        r = c.get("/auth/callback", params=params, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://heylark.dev"


def test_callback_no_next_redirects_to_default():
    now = int(time.time())
    params = _sign_widget(42, now)
    with _make_client() as c:
        r = c.get("/auth/callback", params=params, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://heylark.dev"


def test_callback_cookie_token_is_valid_session():
    """Token issued by callback must decode back to the expected tg_id."""
    from job_hunter import tg_auth

    now = int(time.time())
    params = _sign_widget(42, now)
    params["remember"] = "1"
    with _make_client() as c:
        r = c.get("/auth/callback", params=params, follow_redirects=False)
    token = r.headers["set-cookie"].split("hl_session=", 1)[1].split(";", 1)[0]
    assert tg_auth.read_session(token, secret=TEST_SECRET) == 42
