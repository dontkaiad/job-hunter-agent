"""Tests for POST /api/items/add (the dashboard add-by-URL endpoint).

Overrides get_conn (test DB), get_fx (fake), get_deps (fake LLM Deps) and
get_fetcher (canned page text, no network) via app.dependency_overrides.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from job_hunter import add_by_url, store, webapi
from job_hunter.pipeline import Deps
from job_hunter.states import SURFACED
from tests.conftest import (
    TEST_SUPERUSER_ID,
    FakeFx,
    FakeLLM,
    apply_auth_overrides,
    make_session_cookie,
)

GOOD_TEXT = (
    "Senior Python LLM RAG Engineer. Remote. Claude + FastAPI. "
    "Salary 300000-400000 RUB. Contact @hr. " * 4
)


def _deps() -> Deps:
    fake = FakeLLM()
    fake.set_for(
        "hiring-fit JUDGE",
        '{"relevance_score": 85, "Обоснование": "great fit"}',
    )
    return Deps(llm_client=fake, fx=FakeFx(), use_llm_extract=False)


@pytest.fixture
def fetch_text():
    # Mutable holder so a test can flip the fetcher to "unreadable".
    return {"value": GOOD_TEXT}


@pytest.fixture
def client(conn, auth_conn, fetch_text):
    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    app.dependency_overrides[webapi.get_fx] = lambda: FakeFx()
    app.dependency_overrides[webapi.get_deps] = _deps
    app.dependency_overrides[webapi.get_fetcher] = lambda: (
        lambda _url: fetch_text["value"]
    )
    apply_auth_overrides(app, auth_conn)
    with TestClient(app) as c:
        c.cookies.set("hl_session", make_session_cookie(TEST_SUPERUSER_ID))
        yield c
    app.dependency_overrides.clear()


def test_add_surfaces_card(client, conn):
    resp = client.post("/api/items/add", json={"url": "https://x.example/jobs/1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["duplicate"] is False
    assert body["state"] == SURFACED
    assert body["score"] == 85
    # The card is real and queryable via the existing read endpoint.
    detail = client.get(f"/api/items/{body['item_id']}")
    assert detail.status_code == 200
    assert detail.json()["source"]["channel"] == add_by_url.MANUAL_SOURCE


def test_duplicate_returns_existing(client):
    url = "https://x.example/jobs/2"
    first = client.post("/api/items/add", json={"url": url}).json()
    second = client.post("/api/items/add", json={"url": url})
    assert second.status_code == 200
    body = second.json()
    assert body["duplicate"] is True
    assert body["item_id"] == first["item_id"]


def test_unreadable_returns_422_no_card(client, conn, fetch_text):
    fetch_text["value"] = None  # fetcher yields too little text
    resp = client.post("/api/items/add", json={"url": "https://x.example/js"})
    assert resp.status_code == 422
    assert "read" in resp.json()["detail"].lower()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM work_items WHERE source_channel = 'manual'"
    ).fetchone()["n"]
    assert n == 0


def test_invalid_url_returns_422(client):
    resp = client.post("/api/items/add", json={"url": "not-a-url"})
    assert resp.status_code == 422


def test_requires_auth(conn, auth_conn, fetch_text):
    # No session cookie -> 401 (same gate as every other /api route).
    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    app.dependency_overrides[webapi.get_fx] = lambda: FakeFx()
    app.dependency_overrides[webapi.get_deps] = _deps
    app.dependency_overrides[webapi.get_fetcher] = lambda: (lambda _u: GOOD_TEXT)
    apply_auth_overrides(app, auth_conn)
    with TestClient(app) as c:
        resp = c.post("/api/items/add", json={"url": "https://x.example/jobs/9"})
    app.dependency_overrides.clear()
    assert resp.status_code == 401
