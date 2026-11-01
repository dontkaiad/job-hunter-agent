"""Blind labelling API: auth, the card whitelist, ordering, label upsert, chips rules."""
import json

import pytest
from fastapi.testclient import TestClient

from job_hunter import golden, pipeline, store, webapi
from tests.conftest import (
    TEST_SUPERUSER_ID, FakeFx, FakeLLM, apply_auth_overrides, make_session_cookie,
)


@pytest.fixture
def client(conn, auth_conn):
    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    app.dependency_overrides[webapi.get_fx] = lambda: FakeFx()
    app.dependency_overrides[webapi.get_deps] = lambda: pipeline.Deps(llm_client=FakeLLM(), fx=FakeFx())
    apply_auth_overrides(app, auth_conn)
    with TestClient(app) as c:
        c.cookies.set("hl_session", make_session_cookie(TEST_SUPERUSER_ID))
        yield c
    app.dependency_overrides.clear()


SECRET_NOTE = "СЕКРЕТНОЕ ОБОСНОВАНИЕ"


def _item(conn, n, *, fetched=None):
    ex = {"title": f"Role {n}", "company": "Acme", "stack": ["python"], "seniority": "senior",
          "location": "Berlin", "remote": True, "relocation": False, "salary_min": 3000, "salary_max": 4000,
          "currency": "EUR", "relevance_score": 91.0, "Обоснование": SECRET_NOTE, "reasons": ["r"],
          "source_channel": "@secret_channel", "source_link": "https://t.me/secret_channel/1"}
    iid = store.insert_item(conn, raw_text=f"Role {n} text. Apply https://acme.io/jobs/{n} " + "word " * 200,
                            source_channel="@secret_channel", source_link="https://t.me/secret_channel/1",
                            source_message_id=str(n))
    store.update_state(conn, iid, "extracted", from_state="discovered", kind="deterministic", actor="system",
                       extracted_json=json.dumps(ex, ensure_ascii=False), relevance_score=91.0)
    if fetched:
        conn.execute("UPDATE work_items SET fetched_text=%s WHERE id=%s", (fetched, iid)); conn.commit()
    return iid


def _sample(conn, item_id, wave, order, *, control_of=None):
    return conn.execute(
        "INSERT INTO eval_sample (item_id, wave, order_no, stratum, band, is_control, control_of_item, held_out, created_at) "
        "VALUES (%s,%s,%s,'core','80+',%s,%s,false,'2026-11-02T00:00:00+00:00') RETURNING id",
        (item_id, wave, order, control_of is not None, control_of)).fetchone()["id"]


@pytest.fixture
def two_waves(conn):
    a, b, c = _item(conn, 1), _item(conn, 2), _item(conn, 3)
    ids = [_sample(conn, a, 1, 1), _sample(conn, b, 1, 2), _sample(conn, a, 1, 3, control_of=a), _sample(conn, c, 2, 1)]
    conn.commit()
    return ids


def test_requires_login(conn, auth_conn):
    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    apply_auth_overrides(app, auth_conn)
    with TestClient(app) as c:
        for method, url in (("get", "/api/golden/next"), ("get", "/api/golden/progress"),
                            ("get", "/api/golden/card/1"), ("put", "/api/golden/1/label")):
            assert getattr(c, method)(url).status_code == 401


def test_next_card_is_blind(conn, client, two_waves):
    r = client.get("/api/golden/next")
    card = r.json()["card"]
    assert set(card) == set(golden.CARD_FIELDS)
    blob = json.dumps(r.json(), ensure_ascii=False)
    for leak in (SECRET_NOTE, "secret_channel", "91", "relevance", "Обоснование", "rejected", "extracted", '"item_id"'):
        assert leak not in blob, leak
    assert card["title"] == "Role 1" and card["salary"] == "3000–4000 EUR"
    assert card["link"] == "https://acme.io/jobs/1"                  # the vacancy's own link, not the Telegram one
    assert card["position"] == 1 and card["total"] == 3 and card["wave"] == 1


def test_order_waves_and_progress(conn, client, two_waves):
    seen = []
    while True:
        nxt = client.get("/api/golden/next").json()
        if nxt["card"] is None:
            break
        sid = nxt["card"]["sample_id"]
        seen.append((nxt["card"]["wave"], nxt["card"]["position"]))
        assert client.put(f"/api/golden/{sid}/label", json={"label": "yes"}).status_code == 200
    assert seen == [(1, 1), (1, 2), (1, 3), (2, 1)]                  # wave 1 first, in order
    prog = client.get("/api/golden/progress").json()
    assert prog["finished"] is True and prog["waves"] == [{"wave": 1, "total": 3, "done": 3},
                                                          {"wave": 2, "total": 1, "done": 1}]


def test_control_is_an_ordinary_card_with_its_own_label(conn, client, two_waves):
    first, _, control, _ = two_waves
    client.put(f"/api/golden/{first}/label", json={"label": "yes"})
    client.put(f"/api/golden/{control}/label", json={"label": "no", "chips": ["fit"]})
    rows = conn.execute("select sample_id, label from eval_labels order by sample_id").fetchall()
    assert [(r["sample_id"], r["label"]) for r in rows] == [(first, "yes"), (control, "no")]
    card = client.get(f"/api/golden/card/{control}").json()["card"]
    assert "is_control" not in json.dumps(card) and card["title"] == "Role 1"


def test_label_upsert_and_chip_rules(conn, client, two_waves):
    sid = two_waves[0]
    r = client.put(f"/api/golden/{sid}/label", json={"label": "no", "chips": ["fit", "stale", "bogus"]})
    assert r.status_code == 200
    assert client.get(f"/api/golden/card/{sid}").json()["card"]["chips"] == ["stale", "fit"]
    assert client.get(f"/api/golden/card/{sid}").json()["card"]["label"] == "no"
    client.put(f"/api/golden/{sid}/label", json={"label": "yes", "chips": ["fit"]})     # change of mind
    row = conn.execute("select label, chips, labeled_by from eval_labels where sample_id=%s", (sid,)).fetchone()
    assert (row["label"], row["chips"], row["labeled_by"]) == ("yes", "", str(TEST_SUPERUSER_ID))
    assert conn.execute("select count(*) n from eval_labels").fetchone()["n"] == 1


def test_bad_input(conn, client, two_waves):
    sid = two_waves[0]
    assert client.put(f"/api/golden/{sid}/label", json={"label": "maybe"}).status_code == 422
    assert client.put("/api/golden/99999/label", json={"label": "yes"}).status_code == 404
    assert client.get("/api/golden/card/99999").status_code == 404


def test_page_text_prefers_fetched_page_for_stubs(conn, client):
    iid = _item(conn, 9, fetched="Full vacancy text fetched from the link. " * 20)
    sid = _sample(conn, iid, 1, 1); conn.commit()
    assert client.get(f"/api/golden/card/{sid}").json()["card"]["snippet"].startswith("Full vacancy text")


def test_deleted_item_does_not_break_the_queue(conn, client):
    iid = _item(conn, 5)
    sid = _sample(conn, iid, 1, 1); conn.commit()
    conn.execute("DELETE FROM state_transitions WHERE item_id=%s", (iid,))
    conn.execute("DELETE FROM work_items WHERE id=%s", (iid,)); conn.commit()
    assert client.get("/api/golden/next").json()["card"]["title"] == "(вакансия удалена)"
    assert client.put(f"/api/golden/{sid}/label", json={"label": "unsure"}).status_code == 200


def test_empty_set(client):
    r = client.get("/api/golden/next").json()
    assert r["card"] is None and r["progress"] == {"waves": [], "current_wave": None, "finished": False}
