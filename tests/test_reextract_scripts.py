"""One-off repair scripts for the fallback-extract cohort (2026-11-02..05)."""
import importlib.util
import json
import pathlib
import types

import pytest

from evals import common
from job_hunter import config, llm, store
from job_hunter.profile import example_profile

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "oneoff" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backfill = _load("backfill_extract_mode")
rx = _load("reextract_fallback")


@pytest.fixture(autouse=True)
def _clean_gate():
    common._GATE.clear()
    yield
    common._GATE.clear()


# --- backfill (no API) -------------------------------------------------------

@pytest.mark.parametrize("reason,mode", [
    ("extract (llm+reconcile)", "llm"),
    ("extract (heuristic(fallback))", "heuristic:fallback"),
    ("extract (heuristic(topic-filtered:no AI/ML/LLM keyword in title/prefix))", "heuristic:topic"),
    ("extract (heuristic(prefilter:too short to be a vacancy))", "heuristic:prefilter"),
    ("extract (heuristic(duplicate of #12))", "heuristic:duplicate"),
    ("ingested", None), (None, None), ("extract (something new)", None),
])
def test_mode_from_reason(reason, mode):
    assert backfill.mode_from_reason(reason) == mode


def test_backfill_script_cannot_call_the_api():
    src = (ROOT / "scripts" / "oneoff" / "backfill_extract_mode.py").read_text().lower()
    assert "anthropic" not in src and "llm_extract" not in src and "make_client" not in src


# --- the guarded paid script -------------------------------------------------

@pytest.mark.parametrize("step", ["shadow-check", "reextract-unscored"])
def test_refuses_without_max_usd(step, capsys):
    with pytest.raises(SystemExit) as e:
        rx.main([step])
    assert e.value.code == 2 and "--max-usd" in capsys.readouterr().err


GOOD = json.dumps({"title": "LLM Engineer", "company": "Acme", "stack": ["python"], "seniority": "senior",
                   "remote": True, "relocation": False, "location": None, "salary_min": None, "salary_max": None,
                   "currency": None, "contact": "@hr", "contact_type": "dm", "benefits": [], "reasons": []})


class FakeClient:
    def __init__(self, scores):
        self.scores, self.n_extract, self.n_score = list(scores), 0, 0
        self.meter = common.UsageMeter(max_usd=5.0)

    def complete(self, system, user, **kw):
        self.n_extract += 1
        return GOOD

    def complete_tool(self, *a, **k):
        self.n_score += 1
        s = self.scores.pop(0)
        if s is None:
            return {"relevance_score": None, "insufficient_content": True, "reasoning": "x"}
        return {"relevance_score": s, "insufficient_content": False, "reasoning": f"{s}/100"}


@pytest.fixture
def env(conn, monkeypatch):
    cfg = config.Config(anthropic_api_key="k", database_url="postgresql://unused")
    monkeypatch.setattr(rx, "live_ctx", lambda: (cfg, example_profile(), conn))
    monkeypatch.setattr(store, "connect", lambda dsn: conn)
    return cfg


def _item(conn, mid, mode="heuristic:fallback", state="extracted", created="2026-11-03T07:00:00+00:00"):
    iid = store.insert_item(conn, raw_text=f"Remote LLM engineer {mid}, python, RAG. Apply @hr_{mid}",
                            source_channel="@c", source_message_id=str(mid))
    conn.execute("UPDATE work_items SET state=%s, extract_mode=%s, extracted_json=%s, created_at=%s WHERE id=%s",
                 (state, mode, json.dumps({"title": "heuristic junk"}), created, iid))
    conn.commit()
    return iid


def _shadow(conn, orig_id, score=10.0, kind="score"):
    conn.execute("INSERT INTO eval_shadow (orig_item_id, raw_text, source_channel, score, created_at, kind) "
                 "VALUES (%s, %s, '@c', %s, '2026-11-06T07:00:00+00:00', %s)",
                 (orig_id, f"Remote LLM engineer role {orig_id}, python, RAG, apply by email", score, kind))
    conn.commit()


def test_without_confirm_it_prints_the_estimate_and_makes_no_client(conn, env, monkeypatch, capsys):
    a = _item(conn, 1)
    _shadow(conn, a + 1)
    monkeypatch.setattr(rx, "make_client", lambda *x, **k: (_ for _ in ()).throw(AssertionError("client created")))
    for step in ("shadow-check", "reextract-unscored"):
        with pytest.raises(SystemExit) as e:
            rx.main([step, "--max-usd", "5"])
        assert e.value.code == 0
    out = capsys.readouterr().out
    assert out.count("ESTIMATE $") == 2 and out.count("NO API call was made") == 2


def test_an_estimate_over_the_limit_refuses(conn, env, capsys):
    for n in range(1, 30):
        _item(conn, n)
    with pytest.raises(SystemExit, match="exceeds"):
        rx.main(["reextract-unscored", "--max-usd", "0.001", "--confirm"])


def test_shadow_check_reports_aggregates_and_writes_nothing(conn, env, monkeypatch, capsys):
    ids = [_item(conn, n, state="rejected") for n in range(1, 9)]
    for n in range(4):
        _shadow(conn, ids[0] + 1 + n)                         # inside the cohort id range
    _shadow(conn, 10_000_000, kind="score")                    # outside the range: ignored
    _shadow(conn, ids[0] + 6, kind="topic")                    # inside the range but another kind: ignored
    client = FakeClient([80.0, 30.0, 10.0, None])
    monkeypatch.setattr(rx, "make_client", lambda *a, **k: (common._GATE.update(x=1), client)[1])
    before = conn.execute("select count(*) n from work_items").fetchone()["n"]
    out = rx.run_shadow_check(types.SimpleNamespace(from_date="2026-11-02", to_date="2026-11-05",
                                                     max_usd=5.0, confirm=True))
    assert out["shadow_rows"] == 4 and out["scored"] == 3 and out["outcomes"]["insufficient content"] == 1
    assert out["new_60_plus (would have surfaced)"] == 1
    assert out["new_25_to_59 (would have been kept as rejected)"] == 1
    assert out["new_under_25 (deletion confirmed)"] == 1
    assert conn.execute("select count(*) n from work_items").fetchone()["n"] == before
    assert client.n_extract == 4


def test_reextract_unscored_replaces_only_unscored_fallback_rows(conn, env, monkeypatch):
    fb1, fb2 = _item(conn, 1), _item(conn, 2)
    scored = _item(conn, 3, state="rejected")                  # already scored: untouched
    topic = _item(conn, 4, mode="heuristic:topic")             # deliberate heuristic: untouched
    client = FakeClient([])
    monkeypatch.setattr(rx, "make_client", lambda *a, **k: (common._GATE.update(x=1), client)[1])
    stamp = {i: store.get_item(conn, i).updated_at for i in (fb1, fb2, scored, topic)}
    out = rx.run_reextract_unscored(types.SimpleNamespace(max_usd=5.0, confirm=True))
    assert out["candidates"] == 2 and out["re_extracted"] == 2 and client.n_score == 0   # never scores
    for i in (fb1, fb2):
        it = store.get_item(conn, i)
        assert it.extract_mode == "llm" and '"LLM Engineer"' in it.extracted_json
        assert it.state == "extracted" and it.updated_at == stamp[i]                      # queue order intact
        row = conn.execute("select channel, reason from state_transitions where item_id=%s order by id desc limit 1", (i,)).fetchone()
        assert row["channel"] == "script" and "re-extract" in row["reason"]
    for i in (scored, topic):
        assert store.get_item(conn, i).extracted_json == json.dumps({"title": "heuristic junk"})


def test_a_failed_extract_leaves_the_item_as_it_was(conn, env, monkeypatch):
    fb = _item(conn, 1)

    class Broken(FakeClient):
        def complete(self, *a, **k):
            raise RuntimeError("api down")
    monkeypatch.setattr(rx, "make_client", lambda *a, **k: (common._GATE.update(x=1), Broken([]))[1])
    out = rx.run_reextract_unscored(types.SimpleNamespace(max_usd=5.0, confirm=True))
    assert out["re_extracted"] == 0 and out["left_untouched"] == 1
    assert store.get_item(conn, fb).extract_mode == "heuristic:fallback"
