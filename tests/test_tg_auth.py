"""Unit tests for the framework-agnostic auth primitives (issue #5).

These exercise job_hunter.tg_auth WITHOUT FastAPI: HMAC widget verification,
the signed-session roundtrip, and the grants/authorize path against the
ephemeral auth Postgres (the ``auth_conn`` fixture in conftest.py).
"""

from __future__ import annotations

import hashlib
import hmac
import time

import psycopg
import pytest

from job_hunter import tg_auth

FAKE_BOT_TOKEN = "999999:fake-login-bot-token"


def _sign(data: dict, bot_token: str = FAKE_BOT_TOKEN) -> dict:
    """Return a copy of ``data`` with a correct Telegram-widget ``hash``."""
    dcs = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    h = hmac.new(secret_key, dcs.encode(), hashlib.sha256).hexdigest()
    out = dict(data)
    out["hash"] = h
    return out


def _widget_payload(now: int) -> dict:
    return {
        "id": "42",
        "first_name": "Kai",
        "username": "kai",
        "photo_url": "https://t.me/i/userpic/abc.jpg",
        "auth_date": str(now),
    }


# --- verify_login_widget ----------------------------------------------------


def test_verify_login_widget_valid():
    now = 1_700_000_000
    payload = _sign(_widget_payload(now))
    user = tg_auth.verify_login_widget(payload, FAKE_BOT_TOKEN, now=now + 10)
    assert user is not None
    assert user["id"] == 42 and isinstance(user["id"], int)
    assert user["username"] == "kai"
    assert user["first_name"] == "Kai"
    assert user["auth_date"] == now


def test_verify_login_widget_tampered_hash():
    now = 1_700_000_000
    payload = _sign(_widget_payload(now))
    payload["hash"] = payload["hash"][:-1] + ("0" if payload["hash"][-1] != "0" else "1")
    assert tg_auth.verify_login_widget(payload, FAKE_BOT_TOKEN, now=now) is None


def test_verify_login_widget_tampered_field_invalidates_hash():
    now = 1_700_000_000
    payload = _sign(_widget_payload(now))
    payload["id"] = "9999"  # changed after signing -> HMAC no longer matches
    assert tg_auth.verify_login_widget(payload, FAKE_BOT_TOKEN, now=now) is None


def test_verify_login_widget_stale_auth_date():
    now = 1_700_000_000
    payload = _sign(_widget_payload(now))
    # now far ahead of auth_date -> older than max_age (default 86400s)
    assert tg_auth.verify_login_widget(payload, FAKE_BOT_TOKEN, now=now + 100_000) is None


def test_verify_login_widget_wrong_bot_token():
    now = 1_700_000_000
    payload = _sign(_widget_payload(now), bot_token=FAKE_BOT_TOKEN)
    assert tg_auth.verify_login_widget(payload, "different:token", now=now) is None


def test_verify_login_widget_missing_hash_returns_none():
    now = 1_700_000_000
    payload = _widget_payload(now)  # no hash
    assert tg_auth.verify_login_widget(payload, FAKE_BOT_TOKEN, now=now) is None


def test_verify_login_widget_missing_auth_date_returns_none():
    data = {"id": "42", "first_name": "Kai"}
    payload = _sign(data)
    assert tg_auth.verify_login_widget(payload, FAKE_BOT_TOKEN) is None


def test_verify_login_widget_non_dict_returns_none():
    assert tg_auth.verify_login_widget(None, FAKE_BOT_TOKEN) is None
    assert tg_auth.verify_login_widget("garbage", FAKE_BOT_TOKEN) is None


def test_verify_login_widget_uses_real_clock_when_now_none():
    now = int(time.time())
    payload = _sign(_widget_payload(now))
    user = tg_auth.verify_login_widget(payload, FAKE_BOT_TOKEN)
    assert user is not None and user["id"] == 42


# --- session roundtrip ------------------------------------------------------

SECRET = "unit-secret-A"


def test_session_roundtrip_same_secret():
    token, max_age = tg_auth.issue_session(42, secret=SECRET, remember=False)
    assert tg_auth.read_session(token, secret=SECRET) == 42


def test_session_remember_true_max_age():
    token, max_age = tg_auth.issue_session(42, secret=SECRET, remember=True)
    assert max_age == 2592000


def test_session_remember_false_session_cookie():
    token, max_age = tg_auth.issue_session(42, secret=SECRET, remember=False)
    assert max_age is None


def test_session_different_secret_rejected():
    token, _ = tg_auth.issue_session(42, secret=SECRET, remember=False)
    assert tg_auth.read_session(token, secret="unit-secret-B") is None


def test_session_tampered_token_rejected():
    token, _ = tg_auth.issue_session(42, secret=SECRET, remember=False)
    tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    assert tg_auth.read_session(tampered, secret=SECRET) is None


def test_session_garbage_token_rejected():
    assert tg_auth.read_session("not-a-real-token", secret=SECRET) is None
    assert tg_auth.read_session("", secret=SECRET) is None
    assert tg_auth.read_session(None, secret=SECRET) is None


def test_session_expired_via_tiny_max_age():
    token, _ = tg_auth.issue_session(42, secret=SECRET, remember=True)
    # Read far in the future with the injectable clock -> expired.
    future = int(time.time()) + 10_000_000
    assert tg_auth.read_session(token, secret=SECRET, max_age_seconds=60, now=future) is None


def test_session_within_max_age_ok():
    token, _ = tg_auth.issue_session(42, secret=SECRET, remember=True)
    soon = int(time.time()) + 5
    assert tg_auth.read_session(token, secret=SECRET, max_age_seconds=3600, now=soon) == 42


# --- authorize (against the ephemeral auth PG) ------------------------------


def _seed_grant(conn, tg_id, app, status):
    tg_auth.upsert_grant(conn, tg_id, app, status)


def test_authorize_superuser_always_true_no_row(auth_conn):
    # No grant row at all, but the id is a superuser -> True for ANY app.
    assert tg_auth.authorize(auth_conn, 67686090, "jobhunter", superuser_ids={67686090})
    assert tg_auth.authorize(auth_conn, 67686090, "nexus", superuser_ids={67686090})


def test_authorize_approved_grant_true(auth_conn):
    _seed_grant(auth_conn, 100, "jobhunter", "approved")
    assert tg_auth.authorize(auth_conn, 100, "jobhunter", superuser_ids=set())


def test_authorize_pending_denies(auth_conn):
    _seed_grant(auth_conn, 101, "jobhunter", "pending")
    assert not tg_auth.authorize(auth_conn, 101, "jobhunter", superuser_ids=set())


def test_authorize_denied_denies(auth_conn):
    _seed_grant(auth_conn, 102, "jobhunter", "denied")
    assert not tg_auth.authorize(auth_conn, 102, "jobhunter", superuser_ids=set())


def test_authorize_missing_row_denies(auth_conn):
    assert not tg_auth.authorize(auth_conn, 999, "jobhunter", superuser_ids=set())


def test_authorize_is_app_scoped(auth_conn):
    # Approved for jobhunter must NOT grant nexus.
    _seed_grant(auth_conn, 200, "jobhunter", "approved")
    assert tg_auth.authorize(auth_conn, 200, "jobhunter", superuser_ids=set())
    assert not tg_auth.authorize(auth_conn, 200, "nexus", superuser_ids=set())


def test_authorize_read_only_no_writes(auth_conn):
    # authorize must not create rows.
    tg_auth.authorize(auth_conn, 300, "jobhunter", superuser_ids=set())
    row = auth_conn.execute(
        "SELECT COUNT(*) AS n FROM grants WHERE tg_id = %s", (300,)
    ).fetchone()
    assert row["n"] == 0


# --- schema --------------------------------------------------------------------


def test_init_auth_schema_idempotent(auth_conn):
    # Calling again must not raise.
    tg_auth.init_auth_schema(auth_conn)
    tg_auth.init_auth_schema(auth_conn)
    # invites stub table exists too.
    auth_conn.execute("SELECT * FROM invites")


def test_grants_check_rejects_bad_status(auth_conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        auth_conn.execute(
            "INSERT INTO grants (tg_id, app, status, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (1, "jobhunter", "bogus", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
    auth_conn.rollback()
