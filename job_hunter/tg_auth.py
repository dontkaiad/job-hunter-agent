"""Reusable, framework-agnostic Telegram-Login-Widget auth primitives (issue #5).

This module is the SHARED auth core for the *.heylark.dev apps (job-hunter now,
Nexus/Arcana later). It deliberately imports NO web framework (no FastAPI, no
Starlette) and NO jobhunter-specific config: every secret/dependency
(``bot_token``, ``secret``, the auth-DB connection/``dsn``, ``superuser_ids``,
``now``) is passed in as an ARGUMENT. That way another app can simply
``import tg_auth`` and reuse these functions UNCHANGED for cross-subdomain SSO.

Three concerns, kept separate:

  AUTHENTICATION (identity)
    ``verify_login_widget`` — validate the HMAC-signed payload Telegram's Login
    Widget posts back, proving the caller is the Telegram user they claim.

  SESSION (stateless signed cookie)
    ``issue_session`` / ``read_session`` — mint and verify an ``itsdangerous``
    URL-safe, time-stamped, signed token carrying the tg_id. The SAME
    ``secret`` across heylark apps is what makes the cookie a single-sign-on
    token (shared ``Domain=.heylark.dev``).

  AUTHORIZATION (grants — a SEPARATE auth Postgres)
    ``init_auth_schema`` / ``authorize`` — a ``grants`` table (tg_id, app,
    status) in a DIFFERENT database from the pipeline DB. ``authorize`` is
    READ-ONLY: superusers are always allowed; everyone else needs an
    ``approved`` grant for that ``app``. Kai seeds rows by hand for now.

The only intra-package import is ``job_hunter.clock`` (stdlib-only, also
framework-agnostic) so timestamps match the jobhunter store's ISO-UTC TEXT
convention.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Dict, Optional, Set

import psycopg
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from psycopg.rows import dict_row

from .clock import now_iso

# --- AUTHENTICATION: Telegram Login Widget HMAC verification ----------------

# Telegram's documented widget freshness window default (~24h). Stale payloads
# (replayed old logins) are rejected.
DEFAULT_LOGIN_MAX_AGE_SECONDS = 86400

# Tolerance for a slightly-future auth_date (legitimate client/server clock
# skew). Anything dated further in the future than this is rejected so a forged
# far-future auth_date cannot produce a perpetually-valid payload.
_CLOCK_SKEW_GRACE_SECONDS = 300


def verify_login_widget(
    data: Dict[str, Any],
    bot_token: str,
    *,
    max_age_seconds: int = DEFAULT_LOGIN_MAX_AGE_SECONDS,
    now: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Validate a Telegram Login Widget payload, returning the user dict or None.

    The widget posts back fields like id/first_name/username/photo_url/auth_date
    plus a ``hash``. The HMAC is computed over a "data-check-string" of every
    field EXCEPT ``hash``, sorted by key, each rendered ``key=value``, joined by
    newlines. The key is ``sha256(bot_token)``. We compare in CONSTANT TIME.

    ``bot_token`` MUST be the token of the bot that SIGNS the widget (the login
    bot). Never raises on malformed input — missing ``hash``/``auth_date`` or a
    non-numeric ``auth_date`` simply returns None.

    On success returns the validated user dict with ``id`` coerced to int and
    ``auth_date`` to int (other string fields passed through). On any failure
    (bad/absent hash, wrong token, stale auth_date) returns None.
    """
    if not isinstance(data, dict):
        return None

    received_hash = data.get("hash")
    if not received_hash or not isinstance(received_hash, str):
        return None

    if "auth_date" not in data:
        return None

    # Build the data-check-string from ALL fields except "hash". Values are
    # stringified exactly as Telegram sends them (the widget sends strings).
    pairs = []
    for key in sorted(k for k in data.keys() if k != "hash"):
        value = data[key]
        pairs.append(f"{key}={value}")
    data_check_string = "\n".join(pairs)

    secret_key = hashlib.sha256(bot_token.encode()).digest()
    computed = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    # Constant-time comparison defeats timing attacks on the signature.
    if not hmac.compare_digest(computed, received_hash):
        return None

    # Freshness: reject payloads older than max_age_seconds (replay protection).
    try:
        auth_date = int(data["auth_date"])
    except (TypeError, ValueError):
        return None

    current = int(time.time()) if now is None else int(now)
    # Reject STALE payloads (replay protection) AND payloads dated in the FUTURE
    # by more than a small clock-skew grace. Without the future check, a forged
    # auth_date far in the future yields current - auth_date < 0, which never
    # exceeds max_age_seconds and would validate forever.
    if current - auth_date > max_age_seconds or auth_date - current > _CLOCK_SKEW_GRACE_SECONDS:
        return None

    # Validated identity. Coerce the numeric fields; pass the rest through.
    try:
        tg_id = int(data["id"])
    except (KeyError, TypeError, ValueError):
        return None

    result: Dict[str, Any] = {"id": tg_id, "auth_date": auth_date}
    for opt in ("first_name", "last_name", "username", "photo_url"):
        if opt in data:
            result[opt] = data[opt]
    return result


# --- SESSION: stateless signed cookie ---------------------------------------

# A "remember me" cookie lives ~30 days; an unchecked login is a SESSION cookie
# (no Max-Age -> cleared when the browser closes).
REMEMBER_MAX_AGE_SECONDS = 2592000  # 30 days

# Namespace for the itsdangerous signer (domain separation from other signed
# blobs that might share the same secret).
_SESSION_SALT = "heylark.session"

# Shared SSO cookie name across all *.heylark.dev apps.
SESSION_COOKIE = "hl_session"


def set_session_cookie(response, token: str, max_age: Optional[int], cookie_domain: str) -> None:
    """Set the SSO session cookie with cross-subdomain attributes.

    Framework-agnostic: ``response`` is any object with a ``set_cookie`` method
    (Starlette Response or compatible). Domain=cookie_domain ensures the cookie is
    shared across all *.heylark.dev subdomains. HttpOnly/Secure/SameSite=Lax.
    """
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=max_age,
        domain=cookie_domain,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key=secret, salt=_SESSION_SALT)


def issue_session(tg_id: int, *, secret: str, remember: bool):
    """Mint a signed session token for ``tg_id``.

    Returns ``(token, max_age)``. ``max_age`` is ~30 days when ``remember`` is
    True, or None for a SESSION cookie (browser-lifetime) when False. The caller
    is responsible for setting the cookie with this max_age.
    """
    serializer = _serializer(secret)
    payload = {"tg_id": int(tg_id), "remember": bool(remember)}
    token = serializer.dumps(payload)
    max_age = REMEMBER_MAX_AGE_SECONDS if remember else None
    return token, max_age


def read_session(
    token: str,
    *,
    secret: str,
    max_age_seconds: int = REMEMBER_MAX_AGE_SECONDS,
    now: Optional[int] = None,
) -> Optional[int]:
    """Verify a session token and return its tg_id, or None.

    Returns None for any tampered, expired, or garbage token, and for a token
    signed with a DIFFERENT secret. Never raises. ``now`` (epoch seconds) is
    injectable for tests; when given, the token age is measured against it.
    """
    if not token or not isinstance(token, str):
        return None

    serializer = _serializer(secret)
    # itsdangerous measures age relative to "now". To let tests inject a clock
    # we pass max_age such that a token issued at its embedded timestamp is
    # considered expired when (now - issued) > max_age_seconds. When now is
    # provided we cannot move itsdangerous's clock, so we read the timestamp and
    # apply the age check ourselves.
    try:
        if now is None:
            payload = serializer.loads(token, max_age=max_age_seconds)
        else:
            payload, issued_at = serializer.loads(
                token, max_age=None, return_timestamp=True
            )
            issued_epoch = int(issued_at.timestamp())
            if int(now) - issued_epoch > max_age_seconds:
                return None
    except (SignatureExpired, BadSignature, Exception):
        return None

    if not isinstance(payload, dict):
        return None
    tg_id = payload.get("tg_id")
    if not isinstance(tg_id, int):
        return None
    return tg_id


# --- AUTHORIZATION: grants in the SEPARATE auth Postgres ---------------------


def connect_auth(dsn: str) -> psycopg.Connection:
    """Open a connection to the SHARED auth Postgres (separate from the pipeline
    DB) with ``row_factory=dict_row`` so the web layer and tests share access."""
    return psycopg.connect(dsn, row_factory=dict_row)


def init_auth_schema(conn: psycopg.Connection) -> None:
    """Create the auth-DB schema if absent. Idempotent.

    ``grants`` is the authorization table: (tg_id, app) is unique; ``status`` is
    constrained to pending/approved/denied. Timestamps are ISO-UTC TEXT to match
    the jobhunter store convention. ``invites`` is a deliberately minimal STUB
    table that is UNUSED in this issue (calendar-era invite/bot-approval flow);
    it exists only so the schema is forward-compatible.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grants (
            tg_id      BIGINT NOT NULL,
            app        TEXT   NOT NULL,
            role       TEXT,
            status     TEXT   NOT NULL CHECK (status IN ('pending','approved','denied')),
            created_at TEXT   NOT NULL,
            updated_at TEXT   NOT NULL,
            PRIMARY KEY (tg_id, app)
        )
        """
    )
    # STUB (calendar-era): unused invite/bot-approval flow. Present for forward
    # compatibility only; nothing reads or writes it in this issue.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS invites (
            code       TEXT PRIMARY KEY,
            app        TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def authorize(
    conn: psycopg.Connection,
    tg_id: int,
    app: str,
    *,
    superuser_ids: Set[int],
) -> bool:
    """Return True iff ``tg_id`` may access ``app``. READ-ONLY (no writes here).

    Superusers are always allowed (no grant row needed) for ANY app. Everyone
    else needs an ``approved`` grant scoped to that exact ``app``. A ``pending``
    or ``denied`` status, or no row at all, denies access. Parameterized SQL.
    """
    if tg_id in superuser_ids:
        return True
    row = conn.execute(
        "SELECT 1 FROM grants WHERE tg_id = %s AND app = %s AND status = 'approved'",
        (tg_id, app),
    ).fetchone()
    return row is not None


def upsert_grant(
    conn: psycopg.Connection,
    tg_id: int,
    app: str,
    status: str,
    *,
    role: Optional[str] = None,
) -> None:
    """Insert/update a grant row. NOT used by the request path (authorize is
    read-only); provided so an operator/seed script can grant access with the
    correct ISO-UTC timestamps. ``status`` must be pending/approved/denied (the
    table CHECK enforces it)."""
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO grants (tg_id, app, role, status, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (tg_id, app)
        DO UPDATE SET status = EXCLUDED.status,
                      role = EXCLUDED.role,
                      updated_at = EXCLUDED.updated_at
        """,
        (tg_id, app, role, status, ts, ts),
    )
    conn.commit()
