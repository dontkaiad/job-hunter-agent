"""rescore-labeled: only vacancies without a current-system score, guarded, writes eval_frame only."""
import json
import types

import pytest

from evals import common, golden_eval as ge
from job_hunter import config, store
from job_hunter.profile import example_profile

EX = json.dumps({"title": "LLM Engineer", "company": "Acme", "stack": ["python"]})


def _item(conn, n):
    iid = store.insert_item(conn, raw_text=f"Remote LLM engineer {n}, python RAG, apply by email", source_channel="@c",
                            source_message_id=str(n))
    conn.execute("UPDATE work_items SET extracted_json=%s WHERE id=%s", (EX, iid)); conn.commit()
    return iid


def _sample(conn, item, wave, order):
    conn.execute("INSERT INTO eval_sample (item_id, wave, order_no, stratum, band, is_control, held_out, created_at) "
                 "VALUES (%s,%s,%s,'core','25-39',false,false,'2026-11-08T00:00:00+00:00')", (item, wave, order))
    conn.commit()


@pytest.fixture(autouse=True)
def _gate():
    common._GATE.clear()
    yield
    common._GATE.clear()


@pytest.fixture
def env(conn, monkeypatch):
    cfg = config.Config(anthropic_api_key="k", database_url="postgresql://unused")
    monkeypatch.setattr(ge, "db_ctx", lambda: (cfg, conn))
    monkeypatch.setattr("evals.common.live_ctx", lambda: (cfg, example_profile(), conn))
    monkeypatch.setattr(store, "connect", lambda dsn: conn)
    ids = [_item(conn, n) for n in range(1, 6)]
    for o, i in enumerate(ids, 1):
        _sample(conn, i, 1, o)
    _sample(conn, ids[0], 2, 1)                              # the same vacancy sampled twice (control)
    conn.execute("INSERT INTO eval_frame (item_id, stratum, status, current_score, band, judge_used, cost_usd, scored_at) "
                 "VALUES (%s,'core','scored',40,'40-49',false,0.006,'2026-11-01T00:00:00+00:00')", (ids[0],))
    conn.execute("INSERT INTO eval_frame (item_id, stratum, status, current_score, band, judge_used, cost_usd, scored_at) "
                 "VALUES (%s,'core','insufficient',NULL,NULL,false,0.005,'2026-11-01T00:00:00+00:00')", (ids[1],))
    conn.commit()
    return ids


class FakeClient:
    def __init__(self):
        self.meter, self.n = common.UsageMeter(max_usd=5.0), 0

    def complete_tool(self, *a, **k):
        self.n += 1
        return {"relevance_score": 33, "insufficient_content": False, "reasoning": "x"}


def test_only_unscored_unique_vacancies_are_counted(conn, env):
    rows, total = ge._labelled_todo(conn)
    assert total == 5 and sorted(r["item_id"] for r in rows) == sorted(env[1:])     # the 'insufficient' one is retried
    est = ge.run_estimate_labeled()
    assert est["sampled_vacancies"] == 5 and est["already_scored_on_current_system"] == 1 and est["to_score"] == 4
    assert est["estimate_usd_batch_50pct"] == pytest.approx(est["estimate_usd_sync"] / 2, abs=0.01)


def test_refuses_without_confirm_and_over_limit(conn, env, monkeypatch, capsys):
    monkeypatch.setattr(ge, "make_client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("client created")))
    with pytest.raises(SystemExit) as e:
        ge.run_rescore_labeled(5.0, False)
    assert e.value.code == 0 and "NO API call was made" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="exceeds"):
        ge.run_rescore_labeled(0.0001, True)


def test_rescore_writes_eval_frame_only_and_keeps_existing_scores(conn, env, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(ge, "make_client", lambda *a, **k: (common._GATE.update(x=1), client)[1])
    before = conn.execute("select id, relevance_score, extracted_json from work_items order by id").fetchall()
    out = ge.run_rescore_labeled(5.0, True)
    assert out["done"] == 4 and out["outcomes"]["scored"] == 4 and client.n == 4
    assert conn.execute("select id, relevance_score, extracted_json from work_items order by id").fetchall() == before
    rows = {r["item_id"]: r for r in conn.execute("select * from eval_frame").fetchall()}
    assert rows[env[0]]["current_score"] == 40 and rows[env[0]]["cost_usd"] == 0.006          # untouched
    assert all(rows[i]["status"] == "scored" and rows[i]["current_score"] == 33 and rows[i]["band"] == "25-39" for i in env[1:])


def test_a_rescore_never_overwrites_a_scored_row(conn, env):
    conn.execute("INSERT INTO eval_frame (item_id, stratum, status, current_score, band, judge_used, cost_usd, scored_at) "
                 "VALUES (999,'core','scored',70,'70-79',false,0.0,'2026-11-01T00:00:00+00:00')")
    conn.execute("INSERT INTO eval_frame (item_id, stratum, status, current_score, band, judge_used, cost_usd, scored_at) "
                 "VALUES (999,'core','scored',10,'<25',false,0.0,'x') ON CONFLICT (item_id) DO UPDATE SET current_score = EXCLUDED.current_score "
                 "WHERE eval_frame.status <> 'scored'")
    conn.commit()
    assert conn.execute("select current_score from eval_frame where item_id=999").fetchone()["current_score"] == 70
