"""Tests for the ADDITIVE, GUARDED SPA static mount in webapi.create_app (#6).

The mount serves the built React bundle when DASHBOARD_STATIC_DIR exists. These
tests verify:
  - With a temp static dir (index.html + assets), create_app serves index.html
    at "/" and at unknown SPA routes (e.g. /borderline), serves /assets, BUT the
    API still owns /api: /api/pipeline returns JSON 401 (not index.html) and an
    unknown /api/bogus returns JSON 404 (not index.html).
  - With NO static dir, create_app behaves exactly as today (no catch-all): an
    unknown non-API path is a 404, not index.html.

ZERO-DIFF intent: the existing suite calls create_app() with no /app/static and
must be unaffected; this file only adds coverage. The /api 401 assertions go
through the REAL auth gate (auth overrides applied exactly like the auth tests)
so the 401 is the genuine "no session cookie" response, not a config error.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from job_hunter import webapi
from tests.conftest import apply_auth_overrides

INDEX_HTML = "<!doctype html><html><body><div id='root'>SPA</div></body></html>"
ASSET_JS = "console.log('hashed bundle');"


@pytest.fixture
def static_build(tmp_path):
    """Create a temp static dir mimicking Vite's dist/ (index.html + assets/)."""
    static_dir = tmp_path / "static"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (assets_dir / "index-abc123.js").write_text(ASSET_JS, encoding="utf-8")
    return static_dir


@pytest.fixture
def spa_client(static_build, auth_conn, monkeypatch):
    """A TestClient over an app built WITH the temp static dir mounted.

    Auth deps are overridden (like the auth tests) so the gated /api routes hit
    the real auth gate; with no cookie that is a genuine JSON 401."""
    monkeypatch.setenv("DASHBOARD_STATIC_DIR", str(static_build))
    app = webapi.create_app()
    apply_auth_overrides(app, auth_conn)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_index_served_at_root(spa_client):
    resp = spa_client.get("/")
    assert resp.status_code == 200
    assert "SPA" in resp.text
    assert "text/html" in resp.headers["content-type"]


def test_unknown_spa_route_serves_index(spa_client):
    # A client-side route (React Router owns it) must fall through to index.html.
    resp = spa_client.get("/borderline")
    assert resp.status_code == 200
    assert "SPA" in resp.text


def test_deep_spa_route_serves_index(spa_client):
    resp = spa_client.get("/item/123")
    assert resp.status_code == 200
    assert "SPA" in resp.text


def test_assets_served(spa_client):
    resp = spa_client.get("/assets/index-abc123.js")
    assert resp.status_code == 200
    assert "hashed bundle" in resp.text
    assert "javascript" in resp.headers["content-type"]


def test_api_pipeline_still_json_not_index(spa_client):
    # /api is owned by the API: without a session cookie it is a JSON 401, NOT
    # index.html served by the catch-all.
    resp = spa_client.get("/api/pipeline")
    assert resp.status_code == 401
    assert "application/json" in resp.headers["content-type"]
    assert "SPA" not in resp.text
    assert resp.json()["detail"]


def test_unknown_api_path_json_404_not_index(spa_client):
    # An unknown /api/* path must 404 in JSON via the catch-all guard, not serve
    # index.html (the API owns the /api namespace).
    resp = spa_client.get("/api/bogus")
    assert resp.status_code == 404
    assert "application/json" in resp.headers["content-type"]
    assert "SPA" not in resp.text


def test_login_route_untouched_by_spa(spa_client):
    # The public auth route is registered before the catch-all; it renders the
    # server-side login page, never the SPA index served by the catch-all.
    resp = spa_client.get("/login")
    assert resp.status_code == 200
    assert "SPA" not in resp.text


def test_no_static_dir_is_noop(monkeypatch, auth_conn, tmp_path):
    # Point at a non-existent dir: create_app must NOT register the catch-all, so
    # an unknown non-API path is a plain 404 (FastAPI default), not index.html.
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("DASHBOARD_STATIC_DIR", str(missing))
    app = webapi.create_app()
    apply_auth_overrides(app, auth_conn)
    client = TestClient(app)

    resp = client.get("/borderline")
    assert resp.status_code == 404
    assert "SPA" not in resp.text

    # /api still behaves as today (genuine JSON 401 from the auth gate).
    api_resp = client.get("/api/pipeline")
    assert api_resp.status_code == 401
    assert "application/json" in api_resp.headers["content-type"]
    app.dependency_overrides.clear()


def test_resolve_static_dir_requires_index(monkeypatch, tmp_path):
    # A dir that exists but has NO index.html must resolve to None (no-op).
    empty = tmp_path / "empty-static"
    empty.mkdir()
    monkeypatch.setenv("DASHBOARD_STATIC_DIR", str(empty))
    assert webapi._resolve_static_dir() is None


# ---------------------------------------------------------------------------
# TESTER-ADDED: missing coverage for the SPA-serving correctness boundary.
# ---------------------------------------------------------------------------


def test_api_no_trailing_slash_json_404(spa_client):
    """/api (no trailing slash) must 404 in JSON, not serve index.html.

    The catch-all guard checks ``full_path == "api"`` to prevent leaking
    index.html into the /api namespace when there is no trailing slash."""
    resp = spa_client.get("/api")
    assert resp.status_code == 404
    assert "application/json" in resp.headers["content-type"]
    assert "SPA" not in resp.text


def test_api_trailing_slash_json_404(spa_client):
    """/api/ (trailing slash) must also 404 in JSON, not serve index.html.

    The catch-all guard checks ``full_path.startswith("api/")`` for paths
    like 'api/' (when FastAPI strips the leading slash from the wildcard)."""
    resp = spa_client.get("/api/")
    assert resp.status_code == 404
    assert "application/json" in resp.headers["content-type"]
    assert "SPA" not in resp.text


def test_post_action_still_401_with_spa_mounted(spa_client):
    """POST /api/items/:id/approve must still be gated by require_auth when
    the SPA is mounted — the static mount must NOT bypass the auth dependency."""
    resp = spa_client.post("/api/items/999/approve")
    assert resp.status_code == 401
    assert "application/json" in resp.headers["content-type"]
    assert "SPA" not in resp.text


def test_post_skip_still_401_with_spa_mounted(spa_client):
    """POST /api/items/:id/skip also gates even with SPA mounted."""
    resp = spa_client.post("/api/items/1/skip")
    assert resp.status_code == 401
    assert "application/json" in resp.headers["content-type"]


def test_competencies_spa_route_serves_index(spa_client):
    """/competencies is a client-side SPA route and must return index.html."""
    resp = spa_client.get("/competencies")
    assert resp.status_code == 200
    assert "SPA" in resp.text
    assert "text/html" in resp.headers["content-type"]


def test_auth_callback_not_shadowed_by_spa(spa_client):
    """/auth/callback must be routed to the auth handler, not the SPA catch-all.

    With no valid Telegram params the callback will 401 (bad signature) but
    the response MUST be JSON (the auth route was reached), not HTML index.html
    (which would mean the catch-all shadowed the auth router)."""
    # No query params -> verify_login_widget returns None -> 401
    resp = spa_client.get("/auth/callback")
    assert resp.status_code == 401
    # The auth route raises HTTPException which FastAPI serialises to JSON.
    assert "application/json" in resp.headers["content-type"]
    assert "SPA" not in resp.text


def test_logout_not_shadowed_by_spa(spa_client):
    """POST /logout must be handled by the auth router, not the catch-all."""
    resp = spa_client.post("/logout")
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]
    assert resp.json().get("ok") is True


def test_path_traversal_returns_index_not_file(spa_client):
    """A path traversal attempt (/../etc/passwd) must never expose arbitrary
    files. The catch-all only ever returns the hardcoded index.html because
    FileResponse(index_path) is static — the request path is not used to
    resolve the file. So the response must be index.html content, not passwd."""
    resp = spa_client.get("/../etc/passwd")
    # 200 is acceptable here: the content must be index.html, not the system file.
    assert resp.status_code == 200
    assert "SPA" in resp.text
    # Sanity: no /etc/passwd content leaked.
    assert "root:" not in resp.text


def test_encoded_traversal_returns_index_not_file(spa_client):
    """URL-encoded traversal (/%2e%2e/) must also return index.html only."""
    resp = spa_client.get("/%2e%2e/etc/passwd")
    assert resp.status_code == 200
    assert "SPA" in resp.text
    assert "root:" not in resp.text
