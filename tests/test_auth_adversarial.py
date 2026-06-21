"""Adversarial / security tests for the AUTH layer (issue #5).

Added by the Tester role to cover gaps found during adversarial validation.
Tests are ADDITIVE ONLY — no production code changed.

Gaps covered:

  1. CRITICAL BUG (Bug #1): verify_login_widget accepts a FAR-FUTURE auth_date
     because the staleness check (current - auth_date > max_age) never fails when
     auth_date > current (the result is negative, always < 86400). A signed
     payload with auth_date=year-2100 validates FOREVER.
     Filed for Developer fix; test documents current (wrong) and expected behavior.

  2. Non-numeric auth_date -> None (code is correct but was untested).

  3. hash=None (key present, value None) -> None.

  4. hash="" (empty string) -> None.

  5. hash value of the correct hex length but wrong bytes -> None.

  6. SQL injection attempt on app parameter of authorize() -> denied (param'd SQL).

  7. tg_id=None / tg_id=string do not authorize even with a superuser_ids set match.

  8. /auth/callback with invalid signature -> 401 AND no Set-Cookie header.

  9. /auth/callback valid identity but unauthorized grant -> 403 AND no Set-Cookie.

 10. Route enumeration: EVERY route with path starting /api/ returns 401 without auth
     (dynamic, walks app.routes — not just the 7 hardcoded routes).

 11. Session token issued with remember=False has server-side validity of 30 days
     (the require_auth path uses the default max_age_seconds=2592000). This is an
     architectural note: a stolen session-only token remains valid 30 days on the
     server even though the browser clears it. Marked xfail if the spec ever changes.

 12. PK constraint: upsert_grant on same (tg_id, app) -> ON CONFLICT update (no dup row).

 13. Superuser id bypasses authorization for an app that doesn't even exist in grants.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import psycopg
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

FAKE_BOT_TOKEN = "999999:adversarial-test-bot-token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign(data: dict, bot_token: str = FAKE_BOT_TOKEN) -> dict:
    """Return a copy of data with a correct Telegram-widget hash appended."""
    dcs = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    h = hmac.new(secret_key, dcs.encode(), hashlib.sha256).hexdigest()
    out = dict(data)
    out["hash"] = h
    return out


def _widget_payload(now: int, tg_id: int = 42) -> dict:
    return {
        "id": str(tg_id),
        "first_name": "Adversary",
        "username": "adv",
        "auth_date": str(now),
    }


def _make_app(conn, auth_conn, **settings_over):
    from tests.conftest import _dsn_from_fixture

    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    app.dependency_overrides[webapi.get_fx] = lambda: FakeFx()
    app.dependency_overrides[webapi.get_deps] = lambda: Deps(
        llm_client=FakeLLM(), fx=FakeFx(), use_llm_extract=False
    )
    apply_auth_overrides(app, auth_conn, **settings_over)
    return app


def _seed(conn) -> int:
    """Insert a minimal item; return its id."""
    return store.insert_item(
        conn,
        raw_text="raw",
        source_channel="ch",
        source_link="http://t.me/x/1",
        source_message_id=str(time.time_ns()),
    )


# ---------------------------------------------------------------------------
# 1. CRITICAL BUG: Far-future auth_date bypass
# ---------------------------------------------------------------------------


def test_far_future_auth_date_should_be_rejected():
    """BUG #1 (CRITICAL): verify_login_widget must REJECT a payload whose
    auth_date is set far in the FUTURE (e.g. year 2100).

    Current behaviour (WRONG): passes. The staleness check is:
        current - auth_date > max_age_seconds
    When auth_date > current the left side is NEGATIVE, which is always < 86400,
    so the check never triggers. An attacker who obtains a signed widget callback
    with a future auth_date (e.g. by manipulating the system clock at sign-time
    or by crafting a payload manually against a known bot_token) gets a token
    that is valid FOREVER.

    Expected behaviour: abs(current - auth_date) <= max_age_seconds, OR
    equivalently: reject if auth_date is more than max_age_seconds in the future.

    This test documents the bug and MUST FAIL until the Developer fixes it.
    """
    now = 1_700_000_000  # fixed "current" time for the test
    # auth_date = 100 years in the future
    far_future = now + 100 * 365 * 86400

    payload = _sign(_widget_payload(far_future), FAKE_BOT_TOKEN)
    # The HMAC is correct; only the freshness check should catch this.
    result = tg_auth.verify_login_widget(payload, FAKE_BOT_TOKEN, now=now)

    # BUG: currently returns a user dict instead of None.
    assert result is None, (
        "BUG #1 (CRITICAL): verify_login_widget accepted a far-future auth_date. "
        "A payload with auth_date=year-2100 is valid FOREVER because the staleness "
        "check never fires for negative (current - auth_date). "
        "Fix: reject when auth_date > current + some_grace_period."
    )


def test_auth_date_slightly_in_future_accepted_for_clock_skew():
    """Clock skew of a few seconds should be tolerated (e.g. +30s).

    This is a SEPARATE concern from the far-future bypass: small positive
    clock differences between the Telegram server and the app server are normal.
    Any fix to Bug #1 must preserve this tolerance.
    """
    now = 1_700_000_000
    slight_future = now + 30  # 30 s clock skew

    payload = _sign(_widget_payload(slight_future), FAKE_BOT_TOKEN)
    result = tg_auth.verify_login_widget(payload, FAKE_BOT_TOKEN, now=now)
    # Slight future should pass (within a reasonable grace, e.g. 300s = 5 min).
    # This test is informational; if the fix uses a strict abs check with grace=0
    # then this assertion may need relaxing. We use 30s which is within a typical
    # 5-minute grace window.
    assert result is not None, (
        "Clock skew of 30s into the future should be tolerated (Telegram server "
        "may be slightly ahead of app server). Adjust the fix to allow ±grace."
    )


# ---------------------------------------------------------------------------
# 2. Non-numeric auth_date -> None
# ---------------------------------------------------------------------------


def test_non_numeric_auth_date_returns_none():
    """auth_date='not-a-number' must return None, not crash."""
    payload = _sign({"id": "42", "auth_date": "not-a-number", "first_name": "X"})
    assert tg_auth.verify_login_widget(payload, FAKE_BOT_TOKEN, now=1_700_000_000) is None


def test_float_string_auth_date_returns_none():
    """auth_date='1700000000.5' (float string) must return None; int() fails on it."""
    payload = _sign({"id": "42", "auth_date": "1700000000.5"})
    assert tg_auth.verify_login_widget(payload, FAKE_BOT_TOKEN, now=1_700_000_000) is None


# ---------------------------------------------------------------------------
# 3-4. hash=None and hash="" edge cases
# ---------------------------------------------------------------------------


def test_hash_none_value_returns_none():
    """hash key exists but value is None must return None (not crash on compare_digest)."""
    now = 1_700_000_000
    payload = {"id": "42", "auth_date": str(now), "hash": None}
    assert tg_auth.verify_login_widget(payload, FAKE_BOT_TOKEN, now=now) is None


def test_hash_empty_string_returns_none():
    """hash='' must return None immediately (falsy guard in the implementation)."""
    now = 1_700_000_000
    payload = {"id": "42", "auth_date": str(now), "hash": ""}
    assert tg_auth.verify_login_widget(payload, FAKE_BOT_TOKEN, now=now) is None


# ---------------------------------------------------------------------------
# 5. Correct-length wrong-bytes hash -> None
# ---------------------------------------------------------------------------


def test_correct_length_wrong_bytes_hash_rejected():
    """A 64-hex-char hash with correct length but wrong bytes must be rejected.
    This checks that compare_digest compares values, not just length.
    """
    now = 1_700_000_000
    payload = _sign(_widget_payload(now), FAKE_BOT_TOKEN)
    # Replace hash with 64 correct-length but wrong bytes
    payload["hash"] = "a" * 64
    assert tg_auth.verify_login_widget(payload, FAKE_BOT_TOKEN, now=now) is None


# ---------------------------------------------------------------------------
# 6. SQL injection attempt on app parameter of authorize()
# ---------------------------------------------------------------------------


def test_authorize_sql_injection_app_parameter_denied(auth_conn):
    """A crafted app string 'jobhunter' OR '1'='1' must NOT authorize.
    The SQL is parameterized (%s) so the string is treated as a literal value.
    """
    # Seed an approved grant for the real app name.
    tg_auth.upsert_grant(auth_conn, 500, "jobhunter", "approved")

    # Attempt injection in the app parameter.
    injection = "jobhunter' OR '1'='1"
    result = tg_auth.authorize(auth_conn, 500, injection, superuser_ids=set())
    assert result is False, (
        "SQL injection in app parameter must be denied (parameterized SQL handles it)."
    )

    # Confirm the real app still works.
    assert tg_auth.authorize(auth_conn, 500, "jobhunter", superuser_ids=set()) is True


def test_authorize_sql_injection_tg_id_via_string_cast_denied(auth_conn):
    """tg_id is typed int in Python; passing a non-int raises TypeError before the SQL.
    This documents that the type contract provides a secondary layer of protection.
    """
    tg_auth.upsert_grant(auth_conn, 501, "jobhunter", "approved")
    # tg_id must be int; passing a string that looks like SQL injection
    with pytest.raises((TypeError, psycopg.errors.DataError, Exception)):
        # Psycopg will fail to bind a string where BIGINT is expected,
        # or Python's `in` check will handle it first for superuser check.
        tg_auth.authorize(auth_conn, "501 OR 1=1", "jobhunter", superuser_ids=set())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 7. tg_id type safety in authorize
# ---------------------------------------------------------------------------


def test_authorize_superuser_ids_set_membership_requires_exact_int(auth_conn):
    """A string '67686090' must NOT match the int 67686090 in superuser_ids.
    Python set membership is type-sensitive (str != int).
    """
    # The superuser set contains an int; passing a string tg_id should never match.
    # In practice tg_id comes from read_session which enforces int, but this
    # documents the invariant at the tg_auth.authorize level.
    result = tg_auth.authorize(
        auth_conn, "67686090", "jobhunter", superuser_ids={67686090}  # type: ignore[arg-type]
    )
    # A string is not in a set of ints in Python.
    assert result is False, (
        "String tg_id must not match int superuser_id via set membership."
    )


# ---------------------------------------------------------------------------
# 8. /auth/callback invalid signature -> 401 AND no Set-Cookie
# ---------------------------------------------------------------------------


def test_callback_invalid_signature_no_cookie_set(conn, auth_conn):
    """When /auth/callback returns 401 (bad HMAC), NO cookie must be set.
    Previously the test only checked status_code, not the absence of Set-Cookie.
    """
    app = _make_app(conn, auth_conn)
    now = int(time.time())
    params = {
        "id": str(TEST_SUPERUSER_ID),
        "first_name": "Kai",
        "auth_date": str(now),
        "hash": "deadbeef" * 8,  # 64-char garbage hash
    }
    with TestClient(app) as c:
        r = c.get("/auth/callback", params=params, follow_redirects=False)
        assert r.status_code == 401
        assert "set-cookie" not in r.headers, (
            "401 response from /auth/callback must NOT set any cookie. "
            f"Got Set-Cookie: {r.headers.get('set-cookie')!r}"
        )
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 9. /auth/callback valid identity but denied grant -> 403 AND no Set-Cookie
# ---------------------------------------------------------------------------


def test_callback_unauthorized_no_cookie_set(conn, auth_conn):
    """When /auth/callback returns 403 (authenticated but not authorized), no
    cookie must be set. The session is not issued before the grant check.
    """
    app = _make_app(conn, auth_conn, superuser_ids=set())
    now = int(time.time())
    tg_id = 88888
    # No grant row for this user.
    payload = {
        "id": str(tg_id),
        "first_name": "Denied",
        "auth_date": str(now),
    }
    dcs = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))
    secret_key = hashlib.sha256(TEST_LOGIN_BOT_TOKEN.encode()).digest()
    payload["hash"] = hmac.new(secret_key, dcs.encode(), hashlib.sha256).hexdigest()

    with TestClient(app) as c:
        r = c.get("/auth/callback", params=payload, follow_redirects=False)
        assert r.status_code == 403
        assert "set-cookie" not in r.headers, (
            "403 response (authorized but not granted) must NOT set any cookie. "
            f"Got Set-Cookie: {r.headers.get('set-cookie')!r}"
        )
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 10. Route enumeration: every /api/* route returns 401 without auth
# ---------------------------------------------------------------------------


def test_every_api_route_returns_401_without_auth(conn, auth_conn):
    """Dynamic route enumeration: walk app.routes and assert every route whose
    path starts with '/api/' returns 401 when called without any session cookie.

    This guards against accidentally adding an ungated route in the future.
    """
    item_id = _seed(conn)
    app = _make_app(conn, auth_conn)

    from starlette.routing import Route

    with TestClient(app) as c:  # no cookie
        for route in app.routes:
            if not isinstance(route, Route):
                continue
            if not route.path.startswith("/api/"):
                continue

            # Substitute path param with the seeded item id.
            path = route.path.replace("{item_id}", str(item_id))
            methods = route.methods or {"GET"}

            for method in methods:
                if method in ("HEAD",):
                    continue
                resp = c.request(method, path)
                assert resp.status_code == 401, (
                    f"Route {method} {path} must return 401 without auth cookie, "
                    f"got {resp.status_code}. This route may be ungated."
                )

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 11. Session token (remember=False) has 30-day server-side validity
# ---------------------------------------------------------------------------


def test_session_only_token_server_validity_is_30_days():
    """ARCHITECTURAL FINDING: a session cookie token (remember=False, no Max-Age
    in the browser) still has a 30-day server-side validity window because
    require_auth calls read_session without a max_age_seconds argument, which
    defaults to REMEMBER_MAX_AGE_SECONDS (2592000 = 30 days).

    Consequence: if a 'session' token is exfiltrated (e.g. via XSS or network
    intercept), it remains valid for up to 30 days even though the user did not
    check 'Remember Me'. This is the inherent trade-off of stateless JWTs/signed
    tokens: server-side revocation is not possible.

    This test documents the CURRENT behavior. It is not marked as a bug because
    stateless sessions cannot enforce short server-side expiry without a revocation
    list. The developer should consider documenting this in the security model.
    """
    token, max_age = tg_auth.issue_session(42, secret=TEST_SESSION_SECRET, remember=False)
    assert max_age is None  # browser will not set Max-Age -> session cookie

    # The token itself still has a 30-day server-side window.
    far_future = int(time.time()) + 25 * 86400  # 25 days from now
    result = tg_auth.read_session(
        token,
        secret=TEST_SESSION_SECRET,
        max_age_seconds=tg_auth.REMEMBER_MAX_AGE_SECONDS,  # 30 days (the default)
        now=far_future,
    )
    assert result == 42, (
        "A session-only token (remember=False) has 30-day server-side validity "
        "because require_auth uses the default max_age_seconds=2592000. "
        "This is an architectural note, not necessarily a bug."
    )


# ---------------------------------------------------------------------------
# 12. PK constraint: upsert_grant on same (tg_id, app) -> ON CONFLICT update
# ---------------------------------------------------------------------------


def test_upsert_grant_pk_no_duplicate_rows(auth_conn):
    """INSERT two grants for the same (tg_id, app) must result in ONE row (ON CONFLICT)."""
    tg_auth.upsert_grant(auth_conn, 600, "jobhunter", "pending")
    tg_auth.upsert_grant(auth_conn, 600, "jobhunter", "approved")

    rows = auth_conn.execute(
        "SELECT COUNT(*) AS n, status FROM grants WHERE tg_id = 600 AND app = 'jobhunter' GROUP BY status"
    ).fetchall()
    assert len(rows) == 1, "PK must deduplicate: only one row for (tg_id, app)."
    assert rows[0]["status"] == "approved", "ON CONFLICT should have updated status."
    assert rows[0]["n"] == 1


# ---------------------------------------------------------------------------
# 13. Superuser bypasses for any app (including made-up app names)
# ---------------------------------------------------------------------------


def test_superuser_authorized_for_made_up_app(auth_conn):
    """A superuser id must be authorized for ANY app string, even one that does
    not exist in the grants table and is completely made up.
    """
    assert tg_auth.authorize(
        auth_conn, TEST_SUPERUSER_ID, "nonexistent-app-xyz-abc", superuser_ids={TEST_SUPERUSER_ID}
    ) is True


def test_non_superuser_not_authorized_for_made_up_app(auth_conn):
    """A non-superuser with NO grant row must be denied for a made-up app."""
    assert tg_auth.authorize(
        auth_conn, 700, "nonexistent-app-xyz-abc", superuser_ids=set()
    ) is False


# ---------------------------------------------------------------------------
# 14. Verify the HMAC compare_digest is actually constant-time (not ==)
# ---------------------------------------------------------------------------


def test_hmac_compare_is_constant_time_not_equality():
    """Verify the implementation uses hmac.compare_digest rather than == for the
    HMAC comparison. This is a static code audit (read the source), not a
    timing measurement.
    """
    import inspect

    source = inspect.getsource(tg_auth.verify_login_widget)
    assert "compare_digest" in source, (
        "verify_login_widget must use hmac.compare_digest for the HMAC comparison, "
        "not the == operator, to prevent timing attacks."
    )
    # Confirm == is NOT used for the critical comparison
    # (check that the hash comparison line uses compare_digest, not a plain ==)
    lines = source.splitlines()
    compare_line = next((l for l in lines if "compare_digest" in l), None)
    assert compare_line is not None, "compare_digest call not found in source."
    assert "==" not in compare_line, (
        f"The compare_digest line should not also use ==: {compare_line!r}"
    )


# ---------------------------------------------------------------------------
# 15. DCS excludes ONLY 'hash', not other fields
# ---------------------------------------------------------------------------


def test_dcs_includes_all_fields_except_hash():
    """The data-check-string must include ALL fields except 'hash', sorted by key.
    Adding an extra field after signing invalidates the hash (field is in DCS).
    Removing an existing field also invalidates it (field was in original DCS).
    """
    now = 1_700_000_000
    original = {"id": "42", "auth_date": str(now), "username": "kai"}
    signed = _sign(original, FAKE_BOT_TOKEN)

    # Inject a field after signing -> DCS differs -> hash mismatch
    injected = dict(signed)
    injected["photo_url"] = "http://evil.example/photo.jpg"
    assert tg_auth.verify_login_widget(injected, FAKE_BOT_TOKEN, now=now) is None

    # Remove a field after signing -> DCS differs -> hash mismatch
    missing_field = {k: v for k, v in signed.items() if k != "username"}
    assert tg_auth.verify_login_widget(missing_field, FAKE_BOT_TOKEN, now=now) is None

    # Original signed payload validates correctly (baseline).
    assert tg_auth.verify_login_widget(signed, FAKE_BOT_TOKEN, now=now) is not None


# ---------------------------------------------------------------------------
# 16. Uppercase hash -> rejected
# ---------------------------------------------------------------------------


def test_uppercase_hash_rejected():
    """An uppercase hex hash (same bytes as lowercase) must be rejected.
    HMAC-SHA256 produces lowercase hex; if the widget sends uppercase, the
    compare_digest will fail (different strings). This is correct behavior.
    """
    now = 1_700_000_000
    signed = _sign(_widget_payload(now), FAKE_BOT_TOKEN)
    signed["hash"] = signed["hash"].upper()
    result = tg_auth.verify_login_widget(signed, FAKE_BOT_TOKEN, now=now)
    assert result is None, "Uppercase hex hash must be rejected (case-sensitive compare)."


# ---------------------------------------------------------------------------
# 17. Truncated / garbage / empty session token -> None (no exception)
# ---------------------------------------------------------------------------


def test_read_session_truncated_token_no_exception():
    """A truncated token (first N chars of a valid one) must return None silently."""
    token, _ = tg_auth.issue_session(42, secret=TEST_SESSION_SECRET, remember=False)
    truncated = token[:10]
    assert tg_auth.read_session(truncated, secret=TEST_SESSION_SECRET) is None


def test_read_session_garbage_bytes_no_exception():
    """Garbage that looks like base64 but is not a valid signed token -> None."""
    garbage = "aGVsbG8gd29ybGQ=.fakesig.fakesig"
    assert tg_auth.read_session(garbage, secret=TEST_SESSION_SECRET) is None


# ---------------------------------------------------------------------------
# 18. 403-before-404 ordering: denied user on missing item -> 403
# ---------------------------------------------------------------------------


def test_denied_user_on_missing_item_returns_403_not_404(conn, auth_conn):
    """A user with a DENIED grant who requests a non-existent item must get 403,
    not 404. The auth gate runs before the route handler checks the DB, so no
    data leaks (item existence is not revealed to unauthorized users).

    This is an important security invariant: the authz layer must be evaluated
    BEFORE any data lookup, even for 404 responses.
    """
    tg_id = 900
    tg_auth.upsert_grant(auth_conn, tg_id, "jobhunter", "denied")
    app = _make_app(conn, auth_conn, superuser_ids=set())

    with TestClient(app) as c:
        c.cookies.set("hl_session", make_session_cookie(tg_id))
        # ID 999999 does not exist; a 403 user must NOT discover that via 404.
        r = c.get("/api/items/999999")
        assert r.status_code == 403, (
            f"Denied user on non-existent item: expected 403 (auth before data), "
            f"got {r.status_code}."
        )
    app.dependency_overrides.clear()


def test_pending_user_on_missing_item_returns_403_not_404(conn, auth_conn):
    """Same as above but for a PENDING grant."""
    tg_id = 901
    tg_auth.upsert_grant(auth_conn, tg_id, "jobhunter", "pending")
    app = _make_app(conn, auth_conn, superuser_ids=set())

    with TestClient(app) as c:
        c.cookies.set("hl_session", make_session_cookie(tg_id))
        r = c.get("/api/items/999999")
        assert r.status_code == 403, (
            f"Pending user on non-existent item: expected 403, got {r.status_code}."
        )
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 19. Different serializer salt -> None (domain separation)
# ---------------------------------------------------------------------------


def test_read_session_different_salt_rejected():
    """A token signed with a DIFFERENT salt must be rejected even if the secret
    matches. The _SESSION_SALT provides domain separation so tokens from other
    contexts don't cross-validate.
    """
    from itsdangerous import URLSafeTimedSerializer

    # Mint a token with a different salt (simulating a different app/context).
    wrong_salt_serializer = URLSafeTimedSerializer(
        secret_key=TEST_SESSION_SECRET, salt="different.salt"
    )
    bad_token = wrong_salt_serializer.dumps({"tg_id": 42, "remember": False})

    result = tg_auth.read_session(bad_token, secret=TEST_SESSION_SECRET)
    assert result is None, (
        "Token signed with a different salt must not validate "
        "(itsdangerous includes salt in the HMAC)."
    )


# ---------------------------------------------------------------------------
# 20. Exact tg_id roundtrip — read_session returns what was put in
# ---------------------------------------------------------------------------


def test_read_session_returns_exact_tg_id():
    """read_session must return the EXACT integer tg_id that was signed,
    not a different one (no payload confusion / elevation).
    """
    for tg_id in (1, 67686090, 2**31 - 1, 2**32, 9999999999):
        token, _ = tg_auth.issue_session(tg_id, secret=TEST_SESSION_SECRET, remember=False)
        result = tg_auth.read_session(token, secret=TEST_SESSION_SECRET)
        assert result == tg_id, f"Expected {tg_id!r}, got {result!r}."
        assert isinstance(result, int), f"Must be int, got {type(result).__name__}."
