"""Shared pytest fixtures: ephemeral PostgreSQL store + fakes for LLM/FX.

HOW TO RUN THE TESTS
--------------------
The suite runs against a REAL, ephemeral PostgreSQL using pytest-postgresql's
``postgresql_noproc`` fixture, which connects to an EXTERNALLY-provided Postgres
server and clones a FRESH database per test (function scope) — preserving the
per-test isolation the old in-memory SQLite gave.

This macOS box has no local ``initdb``/``pg_ctl`` on PATH, so we point
``postgresql_noproc`` at a Docker ``postgres:16`` container instead. Start it
once with:

    docker run -d --name jobhunter-test-pg \
        -e POSTGRES_USER=jobhunter -e POSTGRES_PASSWORD=jobhunter \
        -e POSTGRES_DB=jobhunter_test -p 55432:5432 postgres:16

Then run the suite:

    .venv/bin/python -m pytest

The connection coordinates default to that container (host 127.0.0.1, port
55432, user/password ``jobhunter``); override via the PGHOST / PGPORT / PGUSER /
PGPASSWORD env vars if your test Postgres lives elsewhere.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import psycopg
import pytest
from pytest_postgresql import factories

from job_hunter import store
from job_hunter.pipeline import Deps

# Coordinates of the externally-provided test Postgres (the docker postgres:16
# container above). Overridable via env so CI / a different box can repoint it.
_PG_HOST = os.environ.get("PGHOST", "127.0.0.1")
_PG_PORT = int(os.environ.get("PGPORT", "55432"))
_PG_USER = os.environ.get("PGUSER", "jobhunter")
_PG_PASSWORD = os.environ.get("PGPASSWORD", "jobhunter")
# NOTE: this is the per-test working DB that pytest-postgresql CREATES (and
# drops) from a template — it must NOT be a DB that already exists on the server
# (the container's own POSTGRES_DB=jobhunter_test is the maintenance DB it
# connects to). So use a distinct name here.
_PG_DBNAME = os.environ.get("PGDATABASE", "jobhunter_pytest")

# noproc => connect to an already-running server (no initdb/pg_ctl needed). The
# `postgresql` client fixture below clones a fresh DB per test from this.
postgresql_noproc = factories.postgresql_noproc(
    host=_PG_HOST,
    port=_PG_PORT,
    user=_PG_USER,
    password=_PG_PASSWORD,
    dbname=_PG_DBNAME,
)
postgresql = factories.postgresql("postgresql_noproc")

# A SECOND client fixture cloned from the same noproc server, used for the
# SEPARATE auth/grants database in issue #5 tests (it must be a DIFFERENT DB
# from the pipeline DB). pytest-postgresql gives each its own fresh template
# clone, so the two never share state.
postgresql_auth = factories.postgresql(
    "postgresql_noproc", dbname="jobhunter_auth_pytest"
)


def _dsn_from_fixture(pg) -> str:
    """Build a postgresql:// DSN from a pytest-postgresql connection's info."""
    info = pg.info
    return psycopg.conninfo.make_conninfo(
        host=info.host,
        port=info.port,
        user=info.user,
        password=info.password or _PG_PASSWORD,
        dbname=info.dbname,
    )


@pytest.fixture
def pg_dsn(postgresql) -> str:
    """The DSN of the fresh, per-test ephemeral PostgreSQL database.

    Test files that open their OWN store.connect(...) (the inline call-sites)
    request this fixture and pass it where they used to pass a sqlite file path.
    """
    return _dsn_from_fixture(postgresql)


@pytest.fixture
def conn(pg_dsn):
    c = store.connect(pg_dsn)
    store.init_db(c)
    yield c
    c.close()


class FakeLLM:
    """Records calls and returns scripted responses by call index/system text."""

    def __init__(self, responses: Optional[List[str]] = None):
        self.responses = list(responses or [])
        self.calls: List[Dict[str, Any]] = []
        self.by_system: Dict[str, str] = {}

    def set_for(self, system_substr: str, response: str) -> None:
        self.by_system[system_substr] = response

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        model=None,
        cache_system: bool = False,
    ) -> str:
        # Record the structured ``system`` shape the real transport would build,
        # so caching tests can assert on cache_control without a live client.
        from job_hunter.llm import build_system_param

        self.calls.append(
            {
                "system": system,
                "user": user,
                "max_tokens": max_tokens,
                "model": model,
                "cache_system": cache_system,
                "system_param": build_system_param(system, cache_system),
            }
        )
        for key, resp in self.by_system.items():
            if key in system:
                return resp
        if self.responses:
            return self.responses.pop(0)
        return "{}"


class FakeFx:
    """Static RUB-per-unit table; no network."""

    def __init__(self, rates: Optional[Dict[str, float]] = None):
        self.rates = rates or {"RUB": 1.0, "USD": 90.0, "EUR": 100.0}

    def convert(self, amount, currency):
        if amount is None or currency is None:
            return None
        code = currency.upper()
        if code == "RUB":
            return float(amount)
        rate = self.rates.get(code)
        return None if rate is None else float(amount) * rate

    def get_rates_per_rub(self):
        return dict(self.rates)


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def fake_fx():
    return FakeFx()


# --- issue #5 auth test helpers ---------------------------------------------

# A fixed test signing secret + login-bot token + superuser id. Never real.
TEST_SESSION_SECRET = "test-session-secret-do-not-use-in-prod"
TEST_LOGIN_BOT_TOKEN = "123456:test-login-bot-token"
TEST_LOGIN_BOT_USERNAME = "l4rk_test_bot"
TEST_SUPERUSER_ID = 67686090
TEST_COOKIE_DOMAIN = ".heylark.dev"


@pytest.fixture
def auth_conn(postgresql_auth):
    """A connection to the SEPARATE auth/grants Postgres with schema created.

    Distinct DB from the pipeline ``conn`` fixture. Tests seed grants here.
    """
    from job_hunter import tg_auth

    dsn = _dsn_from_fixture(postgresql_auth)
    c = tg_auth.connect_auth(dsn)
    tg_auth.init_auth_schema(c)
    yield c
    c.close()


def make_auth_settings(**over):
    """Build a webapi.AuthSettings with test defaults, overridable per call."""
    from job_hunter import webapi

    base = dict(
        session_secret=TEST_SESSION_SECRET,
        superuser_ids={TEST_SUPERUSER_ID},
        login_bot_token=TEST_LOGIN_BOT_TOKEN,
        login_bot_username=TEST_LOGIN_BOT_USERNAME,
        cookie_domain=TEST_COOKIE_DOMAIN,
        auth_database_url="postgresql://unused-in-tests",
    )
    base.update(over)
    return webapi.AuthSettings(**base)


def apply_auth_overrides(app, auth_conn, **settings_over):
    """Override the webapi auth dependencies on ``app`` to use the test auth DB
    and a test AuthSettings. Returns the AuthSettings used."""
    from job_hunter import webapi

    settings = make_auth_settings(**settings_over)
    app.dependency_overrides[webapi.get_auth_settings] = lambda: settings
    app.dependency_overrides[webapi.get_auth_conn] = lambda: auth_conn
    return settings


def make_session_cookie(tg_id, *, remember=False, secret=TEST_SESSION_SECRET):
    """Mint a valid session cookie token for ``tg_id`` (matches the test secret)."""
    from job_hunter import tg_auth

    token, _ = tg_auth.issue_session(tg_id, secret=secret, remember=remember)
    return token


@pytest.fixture
def deps(fake_llm, fake_fx):
    # Default judge response: a clearly-relevant score so GOOD_POST-style items
    # surface in pipeline/bot tests. Individual tests override via set_for.
    fake_llm.set_for(
        "hiring-fit JUDGE",
        '{"relevance_score": 85, "Обоснование": "applied-LLM role, remote, good stack"}',
    )
    return Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
