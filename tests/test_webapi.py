"""Tests for the READ-ONLY dashboard FastAPI app (issue #3).

Runs against the ephemeral test PostgreSQL (the ``conn`` fixture from
conftest.py). The FastAPI TestClient gets ``get_conn`` and ``get_fx`` overridden
via ``app.dependency_overrides`` so it talks to the test DB and a fake (no
network) FX. Seeding goes through the real ``store`` write functions — the API
itself is verified to never write.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from job_hunter import store, webapi
from job_hunter.states import (
    APPROVED,
    BACKLOG,
    CLOSED,
    DISCOVERED,
    DRAFTED,
    EXTRACTED,
    REJECTED,
    RESEARCHED,
    SCORED,
    SENT,
    SKIPPED,
    SURFACED,
)
from tests.conftest import FakeFx


def _extracted(**over) -> str:
    """Build an extracted_json blob with sane defaults, overridable per item."""
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


def _seed(conn, *, state, score=None, extracted=None, channel="ch", link="http://t.me/x/1"):
    """Insert an item then move it to ``state`` with score + extracted_json.

    Uses real store functions. Returns the new item id. ``DISCOVERED`` items are
    left as inserted (insert_item starts them there) but still get extracted_json
    set so the API has something to parse.
    """
    item_id = store.insert_item(
        conn,
        raw_text="raw job text",
        source_channel=channel,
        source_link=link,
        source_message_id=str(_seed.counter),
    )
    _seed.counter += 1
    if extracted is not None:
        store.set_extracted(conn, item_id, extracted)
    if state != DISCOVERED:
        store.update_state(
            conn,
            item_id,
            state,
            from_state=DISCOVERED,
            kind="deterministic",
            actor="system",
            reason="test seed",
            extracted_json=extracted,
            relevance_score=score,
        )
    elif score is not None:
        # discovered items keep their score via a direct read-path-safe update.
        conn.execute(
            "UPDATE work_items SET relevance_score = %s WHERE id = %s", (score, item_id)
        )
        conn.commit()
    return item_id


_seed.counter = 1


@pytest.fixture
def client(conn):
    """TestClient with get_conn -> test conn and get_fx -> FakeFx (no network)."""
    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    app.dependency_overrides[webapi.get_fx] = lambda: FakeFx()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def client_no_fx(conn):
    """TestClient where FX is absent (get_fx -> None) to assert display=null."""
    app = webapi.create_app()
    app.dependency_overrides[webapi.get_conn] = lambda: conn
    app.dependency_overrides[webapi.get_fx] = lambda: None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# --- shape ------------------------------------------------------------------


def test_pipeline_shape(client, conn):
    _seed(conn, state=SURFACED, score=85.0, extracted=_extracted())
    resp = client.get("/api/pipeline")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list) and len(data) == 1
    it = data[0]
    assert set(it.keys()) == {
        "id",
        "score",
        "company",
        "role",
        "stack",
        "remote",
        "salary",
        "status",
        "published_at",
        "source",
    }
    assert isinstance(it["id"], int)
    assert isinstance(it["score"], float)
    assert it["role"] == "Backend Engineer"
    assert it["company"] == "Acme"
    assert isinstance(it["stack"], list)
    assert it["remote"] is True
    assert it["status"] == SURFACED
    assert isinstance(it["published_at"], str)
    # nested salary
    assert set(it["salary"].keys()) == {"min", "max", "currency", "display"}
    assert it["salary"]["min"] == 200000
    assert it["salary"]["max"] == 300000
    assert it["salary"]["currency"] == "RUB"
    # nested source
    assert set(it["source"].keys()) == {"channel", "link"}
    assert it["source"]["channel"] == "ch"
    assert it["source"]["link"] == "http://t.me/x/1"


# --- filters: status --------------------------------------------------------


def test_filter_status(client, conn):
    _seed(conn, state=SURFACED, score=80.0, extracted=_extracted())
    _seed(conn, state=APPROVED, score=70.0, extracted=_extracted())
    resp = client.get("/api/pipeline", params={"status": APPROVED})
    data = resp.json()
    assert len(data) == 1
    assert data[0]["status"] == APPROVED


# --- filters: min_score (incl. null-score behavior) -------------------------


def test_filter_min_score_excludes_low_and_null(client, conn):
    _seed(conn, state=SURFACED, score=90.0, extracted=_extracted())
    _seed(conn, state=SURFACED, score=40.0, extracted=_extracted())
    _seed(conn, state=DISCOVERED, score=None, extracted=_extracted())  # null score
    resp = client.get("/api/pipeline", params={"min_score": 50})
    data = resp.json()
    assert len(data) == 1
    assert data[0]["score"] == 90.0
    # without the filter all three are returned
    assert len(client.get("/api/pipeline").json()) == 3


# --- filters: remote --------------------------------------------------------


def test_filter_remote(client, conn):
    _seed(conn, state=SURFACED, score=80.0, extracted=_extracted(remote=True))
    _seed(conn, state=SURFACED, score=70.0, extracted=_extracted(remote=False))
    true_items = client.get("/api/pipeline", params={"remote": "true"}).json()
    assert len(true_items) == 1 and true_items[0]["remote"] is True
    false_items = client.get("/api/pipeline", params={"remote": "false"}).json()
    assert len(false_items) == 1 and false_items[0]["remote"] is False


# --- filters: processed (state partition) -----------------------------------


def test_filter_processed_partition(client, conn):
    # unprocessed states
    _seed(conn, state=DISCOVERED, score=10.0, extracted=_extracted())
    _seed(conn, state=EXTRACTED, score=20.0, extracted=_extracted())
    _seed(conn, state=SCORED, score=30.0, extracted=_extracted())
    _seed(conn, state=SURFACED, score=40.0, extracted=_extracted())
    # processed states
    _seed(conn, state=APPROVED, score=50.0, extracted=_extracted())

    unproc = client.get("/api/pipeline", params={"processed": "false"}).json()
    assert {x["status"] for x in unproc} == {DISCOVERED, EXTRACTED, SCORED, SURFACED}

    proc = client.get("/api/pipeline", params={"processed": "true"}).json()
    assert {x["status"] for x in proc} == {APPROVED}


# --- filters: q -------------------------------------------------------------


def test_filter_q_matches_company_role_stack_case_insensitive(client, conn):
    _seed(conn, state=SURFACED, score=80.0, extracted=_extracted(company="Acme", title="Backend", stack=["python"]))
    _seed(conn, state=SURFACED, score=70.0, extracted=_extracted(company="Globex", title="Frontend", stack=["react"]))

    # matches company (case-insensitive)
    by_company = client.get("/api/pipeline", params={"q": "acme"}).json()
    assert len(by_company) == 1 and by_company[0]["company"] == "Acme"

    # matches role
    by_role = client.get("/api/pipeline", params={"q": "frontend"}).json()
    assert len(by_role) == 1 and by_role[0]["company"] == "Globex"

    # matches stack entry
    by_stack = client.get("/api/pipeline", params={"q": "REACT"}).json()
    assert len(by_stack) == 1 and by_stack[0]["company"] == "Globex"

    # non-match excluded
    assert client.get("/api/pipeline", params={"q": "nonexistent-zzz"}).json() == []


# --- filters combine --------------------------------------------------------


def test_filters_combine(client, conn):
    _seed(conn, state=SURFACED, score=90.0, extracted=_extracted())
    _seed(conn, state=SURFACED, score=40.0, extracted=_extracted())
    _seed(conn, state=APPROVED, score=95.0, extracted=_extracted())
    # status=SURFACED AND min_score>=50 -> only the 90.0 surfaced item
    data = client.get("/api/pipeline", params={"status": SURFACED, "min_score": 50}).json()
    assert len(data) == 1
    assert data[0]["status"] == SURFACED and data[0]["score"] == 90.0


# --- salary.display ---------------------------------------------------------


def test_salary_display_present_with_fx(client, conn):
    _seed(conn, state=SURFACED, score=80.0, extracted=_extracted(salary_max=300000, currency="RUB"))
    it = client.get("/api/pipeline").json()[0]
    assert it["salary"]["display"] == "~300k ₽"
    assert it["salary"]["min"] == 200000
    assert it["salary"]["max"] == 300000
    assert it["salary"]["currency"] == "RUB"


def test_salary_display_null_without_fx(client_no_fx, conn):
    _seed(conn, state=SURFACED, score=80.0, extracted=_extracted(salary_max=300000, currency="RUB"))
    it = client_no_fx.get("/api/pipeline").json()[0]
    assert it["salary"]["display"] is None
    # orig values always present
    assert it["salary"]["min"] == 200000
    assert it["salary"]["max"] == 300000
    assert it["salary"]["currency"] == "RUB"


def test_salary_display_foreign_currency_converted(client, conn):
    # USD 1000 -> FakeFx rate 90 -> 90000 RUB -> "~90k ₽"
    _seed(conn, state=SURFACED, score=80.0, extracted=_extracted(salary_min=1000, salary_max=1000, currency="USD"))
    it = client.get("/api/pipeline").json()[0]
    assert it["salary"]["display"] == "~90k ₽"


# --- detail -----------------------------------------------------------------


def test_item_detail_full(client, conn):
    blob = _extracted(
        title="ML Engineer",
        company="DataCorp",
        seniority="senior",
        location="Remote",
        contact_type="dm",
        contact="@hr",
    )
    data = json.loads(blob)
    data[webapi.REASONING_KEY] = "strong applied-LLM match"
    data["draft"] = "Hello, I am interested..."
    data["research"] = {"glassdoor": 4.2}
    item_id = _seed(conn, state=APPROVED, score=88.0, extracted=json.dumps(data, ensure_ascii=False))

    resp = client.get(f"/api/items/{item_id}")
    assert resp.status_code == 200
    d = resp.json()
    assert d["id"] == item_id
    assert d["status"] == APPROVED
    assert d["score"] == 88.0
    assert d["title"] == "ML Engineer"
    assert d["company"] == "DataCorp"
    assert d["seniority"] == "senior"
    assert d["contact_type"] == "dm"
    assert d["contact"] == "@hr"
    assert d["reasoning"] == "strong applied-LLM match"
    assert d["draft"] == "Hello, I am interested..."
    assert d["research"] == {"glassdoor": 4.2}
    assert d["raw_text"] == "raw job text"
    assert d["salary"]["display"] == "~300k ₽"
    # transition history: ingested (insert) + the seed move to APPROVED
    assert len(d["history"]) >= 2
    last = d["history"][-1]
    assert set(last.keys()) == {"from_state", "to_state", "kind", "actor", "reason", "created_at"}
    assert last["to_state"] == APPROVED
    assert last["from_state"] == DISCOVERED


def test_item_detail_404(client, conn):
    resp = client.get("/api/items/999999")
    assert resp.status_code == 404


# --- read-only guarantee ----------------------------------------------------


def test_endpoints_do_not_write(client, conn):
    item_id = _seed(conn, state=SURFACED, score=80.0, extracted=_extracted())
    before = store.get_item(conn, item_id)
    n_before = len(store.list_transitions(conn, item_id))

    client.get("/api/pipeline")
    client.get(f"/api/items/{item_id}")

    after = store.get_item(conn, item_id)
    n_after = len(store.list_transitions(conn, item_id))
    assert after.state == before.state
    assert after.updated_at == before.updated_at
    assert after.relevance_score == before.relevance_score
    assert n_after == n_before


# =============================================================================
# ADVERSARIAL ADDITIONS: gaps in the original test suite
# =============================================================================


# --- remote: null-remote rows must NOT leak into either filtered result -------


def test_filter_remote_null_does_not_leak(client, conn):
    """A row with remote=null MUST be excluded from both remote=true and remote=false results."""
    _seed(conn, state=SURFACED, score=80.0, extracted=_extracted(remote=True))
    _seed(conn, state=SURFACED, score=70.0, extracted=_extracted(remote=False))
    _seed(conn, state=SURFACED, score=60.0, extracted=_extracted(remote=None))  # null row

    true_items = client.get("/api/pipeline", params={"remote": "true"}).json()
    false_items = client.get("/api/pipeline", params={"remote": "false"}).json()
    all_items = client.get("/api/pipeline").json()

    # Without filter: all 3 are returned
    assert len(all_items) == 3

    # null-remote row must not appear in either filtered set
    assert len(true_items) == 1
    assert true_items[0]["remote"] is True

    assert len(false_items) == 1
    assert false_items[0]["remote"] is False


# --- processed partition: ALL 8 processed states, not just APPROVED -----------


def test_filter_processed_partition_all_states(client, conn):
    """processed=true must include ALL 8 non-inbox states; processed=false exactly 4."""
    # Seed one item per processed state
    for state in (SKIPPED, BACKLOG, APPROVED, RESEARCHED, DRAFTED, SENT, CLOSED, REJECTED):
        _seed(conn, state=state, score=50.0, extracted=_extracted())

    # Also seed one unprocessed item
    _seed(conn, state=SURFACED, score=40.0, extracted=_extracted())

    proc = client.get("/api/pipeline", params={"processed": "true"}).json()
    unproc = client.get("/api/pipeline", params={"processed": "false"}).json()

    proc_statuses = {x["status"] for x in proc}
    unproc_statuses = {x["status"] for x in unproc}

    assert proc_statuses == {SKIPPED, BACKLOG, APPROVED, RESEARCHED, DRAFTED, SENT, CLOSED, REJECTED}
    assert unproc_statuses == {SURFACED}

    # The two sets are disjoint
    assert proc_statuses.isdisjoint(unproc_statuses)


# --- min_score=0 with null-score row: null must still be excluded -------------


def test_filter_min_score_zero_excludes_null(client, conn):
    """min_score=0 must EXCLUDE null-score rows (the IS NOT NULL guard must fire even at 0)."""
    _seed(conn, state=SURFACED, score=0.0, extracted=_extracted())   # score=0, included
    _seed(conn, state=SURFACED, score=None, extracted=_extracted())  # null score, excluded

    data = client.get("/api/pipeline", params={"min_score": 0}).json()
    assert len(data) == 1
    assert data[0]["score"] == 0.0


# --- min_score: null-score rows INCLUDED when filter absent -------------------


def test_filter_min_score_absent_includes_null(client, conn):
    """Without min_score filter, rows with null relevance_score ARE returned."""
    _seed(conn, state=SURFACED, score=None, extracted=_extracted())
    data = client.get("/api/pipeline").json()
    assert len(data) == 1
    assert data[0]["score"] is None


# --- status: unknown status -> empty list, not 500 ---------------------------


def test_filter_status_unknown_returns_empty(client, conn):
    """A status value that matches no state must return [] (200), not an error."""
    _seed(conn, state=SURFACED, score=80.0, extracted=_extracted())
    resp = client.get("/api/pipeline", params={"status": "does_not_exist_zz"})
    assert resp.status_code == 200
    assert resp.json() == []


# --- non-int item_id -> 422 (not 500) ----------------------------------------


def test_item_detail_non_int_id_422(client):
    """A non-integer path segment must yield 422 (FastAPI type validation), not 500."""
    resp = client.get("/api/items/notanumber")
    assert resp.status_code == 422


def test_item_detail_zero_id_404(client, conn):
    """id=0 cannot exist (SERIAL starts at 1) -> 404."""
    resp = client.get("/api/items/0")
    assert resp.status_code == 404


# --- minimal / empty extracted_json -> no 500 --------------------------------


def test_item_detail_empty_extracted_json_no_500(client, conn):
    """A row with extracted_json=NULL must return 200 with graceful null/[] defaults."""
    item_id = store.insert_item(
        conn,
        raw_text="raw post",
        source_channel="ch",
        source_link="http://t.me/x/99",
        source_message_id="minimal-null-json",
    )
    # extracted_json stays NULL (not set)
    resp = client.get(f"/api/items/{item_id}")
    assert resp.status_code == 200
    d = resp.json()
    assert d["stack"] == []
    assert d["reasons"] == []
    assert d["benefits"] == []
    assert d["company"] is None
    assert d["title"] is None
    assert d["remote"] is None
    assert d["reasoning"] is None
    assert d["draft"] is None


def test_pipeline_empty_extracted_json_no_500(client, conn):
    """A row with extracted_json=NULL must appear in the pipeline list without error."""
    store.insert_item(
        conn,
        raw_text="raw post",
        source_channel="ch",
        source_link="http://t.me/x/100",
        source_message_id="minimal-null-pipeline",
    )
    resp = client.get("/api/pipeline")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    it = data[0]
    assert it["stack"] == []
    assert it["company"] is None
    assert it["role"] is None
    assert it["remote"] is None


def test_pipeline_minimal_extracted_json_missing_keys(client, conn):
    """Rows where extracted_json is present but missing benefits/draft/Обоснование are graceful."""
    minimal = json.dumps({"title": "Dev", "source_channel": "ch"})
    item_id = _seed(conn, state=SURFACED, score=55.0, extracted=minimal)
    resp = client.get("/api/pipeline")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1

    detail = client.get(f"/api/items/{item_id}").json()
    assert detail["benefits"] == []
    assert detail["reasoning"] is None
    assert detail["draft"] is None
    assert detail["research"] is None


# --- ordering: null-score rows sort LAST -------------------------------------


def test_ordering_null_score_last(client, conn):
    """Rows with null relevance_score must appear AFTER scored rows (NULLS LAST)."""
    _seed(conn, state=SURFACED, score=30.0, extracted=_extracted(company="Low"))
    _seed(conn, state=SURFACED, score=None, extracted=_extracted(company="Null"))
    _seed(conn, state=SURFACED, score=90.0, extracted=_extracted(company="High"))

    data = client.get("/api/pipeline").json()
    assert len(data) == 3

    # Null score must be LAST
    assert data[-1]["company"] == "Null"
    assert data[-1]["score"] is None

    # Highest score first
    assert data[0]["company"] == "High"
    assert data[0]["score"] == 90.0


# --- salary: no-salary row has null min/max/currency/display -----------------


def test_salary_absent_all_null(client, conn):
    """An item with no salary info must return null for all salary sub-fields."""
    no_salary = json.dumps({
        "title": "Dev",
        "source_channel": "ch",
        "company": "NoPay",
        "stack": ["python"],
        "remote": True,
        "reasons": [],
    })
    _seed(conn, state=SURFACED, score=70.0, extracted=no_salary)
    data = client.get("/api/pipeline").json()
    assert len(data) == 1
    sal = data[0]["salary"]
    assert sal["min"] is None
    assert sal["max"] is None
    assert sal["currency"] is None
    assert sal["display"] is None


# --- salary display with non-RUB currency in detail endpoint -----------------


def test_salary_display_non_rub_in_detail(client, conn):
    """EUR salary must be converted to RUB display in the detail endpoint."""
    # FakeFx: EUR -> 100 RUB/unit. salary_max=2000 EUR -> 200000 RUB -> ~200k ₽
    eur_blob = json.dumps({
        "title": "Senior Dev",
        "source_channel": "ch",
        "company": "EuroCorp",
        "stack": ["python"],
        "salary_min": 1000,
        "salary_max": 2000,
        "currency": "EUR",
        "remote": True,
        "reasons": [],
    })
    item_id = _seed(conn, state=APPROVED, score=88.0, extracted=eur_blob)
    resp = client.get(f"/api/items/{item_id}")
    assert resp.status_code == 200
    d = resp.json()
    assert d["salary"]["currency"] == "EUR"
    assert d["salary"]["min"] == 1000
    assert d["salary"]["max"] == 2000
    # FakeFx EUR rate = 100 -> 2000 * 100 = 200000 -> "~200k ₽"
    assert d["salary"]["display"] == "~200k ₽"


# --- q: whitespace-only behavior is sane ------------------------------------


def test_filter_q_whitespace_not_crashy(client, conn):
    """A whitespace-only q must not 500; it may match broadly but must not error."""
    _seed(conn, state=SURFACED, score=80.0, extracted=_extracted(company="Acme"))
    resp = client.get("/api/pipeline", params={"q": "   "})
    assert resp.status_code == 200


def test_filter_q_empty_string_returns_all(client, conn):
    """An empty-string q is treated as absent (returns all items)."""
    _seed(conn, state=SURFACED, score=80.0, extracted=_extracted(company="Acme"))
    _seed(conn, state=SURFACED, score=70.0, extracted=_extracted(company="Globex"))
    resp = client.get("/api/pipeline", params={"q": ""})
    assert resp.status_code == 200
    # empty string -> falsy -> no filter
    assert len(resp.json()) == 2


# --- q: SQL-injection-like strings are safe (Python filter, not SQL LIKE) ---


def test_filter_q_percent_treated_literally(client, conn):
    """A q='%' must not crash or match everything via SQL injection; treated literally."""
    _seed(conn, state=SURFACED, score=80.0, extracted=_extracted(company="Acme", stack=["python"]))
    resp = client.get("/api/pipeline", params={"q": "%"})
    assert resp.status_code == 200
    # "%" is not in "acme backend engineer python" haystack
    assert resp.json() == []


def test_filter_q_sql_injection_literal(client, conn):
    """q with SQL metacharacters must be treated as literal Python substring."""
    _seed(conn, state=SURFACED, score=80.0, extracted=_extracted(company="Acme"))
    resp = client.get("/api/pipeline", params={"q": "' OR 1=1 --"})
    assert resp.status_code == 200
    assert resp.json() == []  # no match; not an SQL injection


# --- filters combine: all 4 filters at once ----------------------------------


def test_filters_combine_all_four(client, conn):
    """status + min_score + remote + q INTERSECT (not union)."""
    # This item matches all 4
    _seed(conn, state=SURFACED, score=90.0, extracted=_extracted(
        company="Target", title="ML Engineer", stack=["pytorch"], remote=True
    ))
    # Matches status+remote+q but not min_score
    _seed(conn, state=SURFACED, score=30.0, extracted=_extracted(
        company="Target", remote=True
    ))
    # Matches status+min_score+remote but not q
    _seed(conn, state=SURFACED, score=90.0, extracted=_extracted(
        company="Other", title="Backend", stack=["java"], remote=True
    ))
    # Matches everything but wrong status
    _seed(conn, state=APPROVED, score=90.0, extracted=_extracted(
        company="Target", title="ML Engineer", remote=True
    ))

    data = client.get("/api/pipeline", params={
        "status": SURFACED,
        "min_score": 50,
        "remote": "true",
        "q": "target",
    }).json()

    assert len(data) == 1
    assert data[0]["company"] == "Target"
    assert data[0]["score"] == 90.0


# --- history ordering: transitions in chronological order --------------------


def test_history_order_chronological(client, conn):
    """Transitions in history must be in insertion order (first=oldest)."""
    item_id = _seed(conn, state=APPROVED, score=85.0, extracted=_extracted())
    resp = client.get(f"/api/items/{item_id}")
    assert resp.status_code == 200
    history = resp.json()["history"]
    # At least 2 entries: ingested + move to APPROVED
    assert len(history) >= 2
    # First entry: ingested (to_state = discovered, from_state = null)
    assert history[0]["from_state"] is None
    assert history[0]["to_state"] == DISCOVERED
    # Last entry: moved to APPROVED
    assert history[-1]["to_state"] == APPROVED


# --- read-only: 50 rapid requests exhaust no connections and change nothing --


def test_read_only_50_requests_no_write_no_leak(client, conn):
    """Fire 50 requests to both endpoints; DB state must remain unchanged and no exception."""
    item_id = _seed(conn, state=SURFACED, score=80.0, extracted=_extracted())
    before = store.get_item(conn, item_id)
    n_before = len(store.list_transitions(conn, item_id))

    for _ in range(25):
        r1 = client.get("/api/pipeline")
        r2 = client.get(f"/api/items/{item_id}")
        assert r1.status_code == 200
        assert r2.status_code == 200

    after = store.get_item(conn, item_id)
    n_after = len(store.list_transitions(conn, item_id))

    assert after.state == before.state
    assert after.updated_at == before.updated_at
    assert after.relevance_score == before.relevance_score
    assert n_after == n_before


# --- list endpoint must NOT include raw_text or detail-only fields -----------


def test_pipeline_list_excludes_detail_fields(client, conn):
    """PipelineItem must not expose raw_text, draft, reasoning, research, history."""
    _seed(conn, state=SURFACED, score=80.0, extracted=_extracted())
    data = client.get("/api/pipeline").json()
    assert len(data) == 1
    it = data[0]
    forbidden = {"raw_text", "draft", "reasoning", "research", "history",
                 "seniority", "relocation", "location", "contact_type", "contact",
                 "reasons", "benefits", "updated_at"}
    leaked = forbidden & set(it.keys())
    assert not leaked, f"Detail-only fields leaked into list response: {leaked}"


# --- published_at == created_at (ingestion time) confirmed -------------------


def test_published_at_equals_created_at(client, conn):
    """published_at in the list endpoint must equal the work_items.created_at column."""
    item_id = _seed(conn, state=SURFACED, score=80.0, extracted=_extracted())
    row = store.get_item(conn, item_id)

    data = client.get("/api/pipeline").json()
    assert len(data) == 1
    assert data[0]["published_at"] == row.created_at

    detail = client.get(f"/api/items/{item_id}").json()
    assert detail["published_at"] == row.created_at


def test_filter_q_single_space_is_sane(client, conn):
    """q=' ' (single space) should not match real items — whitespace is not meaningful text.

    Whitespace-only q is stripped and treated as ABSENT (no filter -> return all),
    which is consistent with empty-string q (see test_filter_q_empty_string_returns_all).
    The earlier bug returned a non-deterministic subset (only items whose haystack
    happened to contain a literal space); stripping q first fixes it.
    """
    _seed(conn, state=SURFACED, score=80.0, extracted=_extracted(company="Acme Corp"))
    _seed(conn, state=SURFACED, score=70.0, extracted=_extracted(company="Globex Ltd"))

    resp = client.get("/api/pipeline", params={"q": " "})
    assert resp.status_code == 200
    # Stripped to empty -> behaves like no filter -> all items returned.
    assert len(resp.json()) == 2, (
        "whitespace-only q should be stripped and treated as absent (return all)"
    )
