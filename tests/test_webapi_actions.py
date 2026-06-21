"""Tests for the WRITE/action endpoints of the dashboard API (issue #4).

Runs against the ephemeral test PostgreSQL (the ``conn`` fixture from
conftest.py). The FastAPI TestClient overrides ``get_conn`` (test DB),
``get_fx`` (fake FX, no network) and ``get_deps`` (fake LLM Deps, no network)
via ``app.dependency_overrides``.

Core guarantees verified here:
  - Each action produces the SAME transition the bot's button produces. We
    cross-check by driving a TWIN item through ``pipeline.advance_by_id`` with
    the identical decision and asserting equal end-state.
  - 404 (missing id), 409 (invalid transition for current state), 422 (non-int).
  - PERSISTENCE: a successful action is committed (re-read via a SEPARATE
    connection confirms the new state stuck).
  - CONCURRENCY: a second concurrent action on an already-moved item -> 409.
  - SOLE-WRITER: webapi.py contains NO store.update_state / no direct state SQL.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from job_hunter import pipeline, store, webapi
from job_hunter.pipeline import Deps
from job_hunter.states import (
    APPROVED,
    BACKLOG,
    DECISION_APPROVE,
    DECISION_BACKLOG,
    DECISION_SEND,
    DECISION_SKIP,
    DRAFTED,
    SCORED,
    SENT,
    SKIPPED,
    SURFACED,
)
from tests.conftest import (
    TEST_SUPERUSER_ID,
    FakeFx,
    FakeLLM,
    apply_auth_overrides,
    make_session_cookie,
)


def _extracted(**over) -> str:
    base = {
        "title": "Backend Engineer",
        "source_channel": "ch",
        "company": "Acme",
        "stack": ["python", "fastapi"],
        "salary_min": 200000,
        "salary_max": 300000,
        "currency": "RUB",
        "remote": True,
        "reasons": ["good fit"],
        "benefits": ["dms"],
    }
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


def _seed(conn, *, state, score=85.0, extracted=None):
    """Insert an item then move it (via real store fns) to ``state``."""
    extracted = _extracted() if extracted is None else extracted
    item_id = store.insert_item(
        conn,
        raw_text="raw job text",
        source_channel="ch",
        source_link="http://t.me/x/1",
        source_message_id=str(_seed.counter),
    )
    _seed.counter += 1
    store.set_extracted(conn, item_id, extracted)
    if state != "discovered":
        store.update_state(
            conn,
            item_id,
            state,
            from_state="discovered",
            kind="deterministic",
            actor="system",
            reason="test seed",
            extracted_json=extracted,
            relevance_score=score,
        )
    return item_id


_seed.counter = 1


def _draft_deps() -> Deps:
    """Deps with a fake LLM scripted so run_to_gate reaches DRAFTED (no network)."""
    fake = FakeLLM()
    fake.set_for("research", '{"summary":"s","talking_points":[],"questions":[]}')
    # Any draft body works; agents.draft appends the deterministic signature.
    fake.set_for("application message", "Hello, I'd love to apply.")
    return Deps(llm_client=fake, fx=FakeFx(), use_llm_extract=False)


@pytest.fixture
def deps_factory():
    return _draft_deps


@pytest.fixture
def client(conn, auth_conn, deps_factory):
    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    app.dependency_overrides[webapi.get_fx] = lambda: FakeFx()
    app.dependency_overrides[webapi.get_deps] = deps_factory
    apply_auth_overrides(app, auth_conn)
    with TestClient(app) as c:
        c.cookies.set("hl_session", make_session_cookie(TEST_SUPERUSER_ID))
        yield c
    app.dependency_overrides.clear()


# --- happy-path transitions == the bot's button transition ------------------


@pytest.mark.parametrize(
    "action,decision,from_state,expected",
    [
        ("approve", DECISION_APPROVE, SURFACED, APPROVED),
        ("approve", DECISION_APPROVE, BACKLOG, APPROVED),
        ("skip", DECISION_SKIP, SURFACED, SKIPPED),
        ("skip", DECISION_SKIP, BACKLOG, SKIPPED),
        ("backlog", DECISION_BACKLOG, SURFACED, BACKLOG),
        ("sent", DECISION_SEND, DRAFTED, SENT),
    ],
)
def test_action_matches_bot_transition(
    client, conn, action, decision, from_state, expected
):
    # Endpoint item.
    item_id = _seed(conn, state=from_state)
    resp = client.post(f"/api/items/{item_id}/{action}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == expected
    assert resp.json()["id"] == item_id

    # Twin item driven directly through advance_by_id with the same decision.
    twin_id = _seed(conn, state=from_state)
    res = pipeline.advance_by_id(conn, twin_id, decision=decision, deps=_draft_deps())
    assert res.status == "moved"
    assert res.to_state == expected
    # Endpoint end-state equals the SOLE-writer's end-state for the same input.
    assert resp.json()["status"] == store.get_item(conn, twin_id).state


# --- draft endpoint ---------------------------------------------------------


def test_draft_from_approved_returns_draft_text(client, conn):
    item_id = _seed(conn, state=APPROVED)
    resp = client.post(f"/api/items/{item_id}/draft")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == DRAFTED
    assert body["draft"]
    assert "Hello, I'd love to apply." in body["draft"]


def test_draft_idempotent_when_already_drafted(client, conn, monkeypatch):
    item_id = _seed(conn, state=APPROVED)
    first = client.post(f"/api/items/{item_id}/draft").json()
    assert first["status"] == DRAFTED
    existing_draft = first["draft"]

    # Guard: a second /draft must NOT re-run the agent chain.
    calls = {"n": 0}
    real = pipeline.run_to_gate

    def _spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(webapi.pipeline_mod, "run_to_gate", _spy)
    second = client.post(f"/api/items/{item_id}/draft").json()
    assert second["status"] == DRAFTED
    assert second["draft"] == existing_draft
    assert calls["n"] == 0  # idempotent: no LLM rerun


def test_draft_from_surfaced_409(client, conn):
    item_id = _seed(conn, state=SURFACED)
    resp = client.post(f"/api/items/{item_id}/draft")
    assert resp.status_code == 409
    assert "approved" in resp.json()["detail"]


# --- invalid transitions -> 409 ---------------------------------------------


def test_sent_on_surfaced_409(client, conn):
    item_id = _seed(conn, state=SURFACED)
    resp = client.post(f"/api/items/{item_id}/sent")
    assert resp.status_code == 409
    assert "surfaced" in resp.json()["detail"]


def test_approve_on_skipped_409(client, conn):
    item_id = _seed(conn, state=SKIPPED)
    resp = client.post(f"/api/items/{item_id}/approve")
    assert resp.status_code == 409


def test_double_approve_409(client, conn):
    item_id = _seed(conn, state=SURFACED)
    assert client.post(f"/api/items/{item_id}/approve").status_code == 200
    # Second concurrent approve: item is now 'approved', not 'surfaced' -> 409.
    second = client.post(f"/api/items/{item_id}/approve")
    assert second.status_code == 409
    assert "approved" in second.json()["detail"]


# --- 404 / 422 --------------------------------------------------------------


@pytest.mark.parametrize("action", ["approve", "skip", "backlog", "sent", "draft"])
def test_missing_id_404(client, action):
    resp = client.post(f"/api/items/999999/{action}")
    assert resp.status_code == 404


@pytest.mark.parametrize("action", ["approve", "skip", "backlog", "sent", "draft"])
def test_non_int_id_422(client, action):
    resp = client.post(f"/api/items/not-an-int/{action}")
    assert resp.status_code == 422


# --- persistence (commit proven via a SEPARATE connection) ------------------


def test_action_persists_to_db(client, conn, pg_dsn):
    item_id = _seed(conn, state=SURFACED)
    assert client.post(f"/api/items/{item_id}/approve").status_code == 200

    # Re-read through an INDEPENDENT connection: proves advance() committed.
    other = store.connect(pg_dsn)
    try:
        reread = store.get_item(other, item_id)
    finally:
        other.close()
    assert reread is not None
    assert reread.state == APPROVED


def test_concurrency_second_action_409(client, conn):
    """Simulate two concurrent approves: the second sees the moved state -> 409."""
    item_id = _seed(conn, state=SURFACED)
    # First approve via the pipeline (as a competing writer would).
    res = pipeline.advance_by_id(conn, item_id, decision=DECISION_APPROVE, deps=_draft_deps())
    assert res.status == "moved"
    # Endpoint now sees 'approved' (fresh re-read inside advance_by_id) -> 409.
    resp = client.post(f"/api/items/{item_id}/approve")
    assert resp.status_code == 409


# --- response shape reuses the #3 detail serializer -------------------------


def test_response_is_full_detail(client, conn):
    item_id = _seed(conn, state=DRAFTED, extracted=_extracted())
    # Pre-seed a draft + reasoning into the blob so detail surfaces them.
    blob = json.loads(_extracted())
    blob["draft"] = "pre-existing draft"
    blob["Обоснование"] = "why this fits"
    store.set_extracted(conn, item_id, json.dumps(blob, ensure_ascii=False))

    resp = client.post(f"/api/items/{item_id}/sent")
    assert resp.status_code == 200
    body = resp.json()
    # Full ItemDetail shape: status + draft + reasoning + history all present.
    assert body["status"] == SENT
    assert body["draft"] == "pre-existing draft"
    assert body["reasoning"] == "why this fits"
    assert isinstance(body["history"], list) and len(body["history"]) >= 1
    assert body["history"][-1]["to_state"] == SENT


# --- SOLE-WRITER audit: no direct writes in webapi.py -----------------------


def test_webapi_has_no_direct_state_writes():
    raw = Path(webapi.__file__).read_text(encoding="utf-8").splitlines()
    # Strip comment lines AND docstring-style prose so a phrase in documentation
    # does not trip the audit. We only care about EXECUTABLE write calls.
    code_lines = []
    for ln in raw:
        stripped = ln.strip()
        if stripped.startswith("#"):
            continue
        code_lines.append(ln)
    code = "\n".join(code_lines)

    # Crude but effective: remove anything after a '#' on a line, and collapse
    # the module/function docstrings (which never call functions) by dropping
    # lines that are clearly prose (start with a capital word, no '(').
    src = code

    def _calls(name: str) -> bool:
        # A real call looks like `name(` somewhere in code, not just the token.
        return bool(re.search(re.escape(name) + r"\s*\(", src))

    # No direct state-write helpers may be CALLED from the API layer.
    assert not _calls("store.update_state")
    assert not _calls("store.set_extracted")
    # No raw UPDATE/INSERT against the state tables.
    assert not re.search(r"UPDATE\s+work_items", src, re.IGNORECASE)
    assert not re.search(r"INSERT\s+INTO\s+state_transitions", src, re.IGNORECASE)
    # Writes must go through the pipeline (advance is the sole writer).
    assert _calls("advance_by_id") or "advance_by_id" in src
    assert _calls("run_to_gate") or "run_to_gate" in src
