"""Offline evals in CI (synthetic, no API / network / DB) + guards on the eval harness itself.

Live modes (paid API, production DB) are NOT exercised here and never run in CI.
"""
from __future__ import annotations

import json
import re
import socket
from pathlib import Path

import pytest

from evals import common
from evals.__main__ import all_ok, run_all

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_FILES = (
    list((ROOT / "evals").rglob("*.py")) + list((ROOT / "evals").rglob("*.json"))
    + list((ROOT / "docs" / "CASES").glob("*.md"))
)


def test_offline_evals_pass_without_network(monkeypatch):
    def no_net(*a, **k):
        raise AssertionError("offline eval touched the network")
    monkeypatch.setattr(socket.socket, "connect", no_net)
    monkeypatch.setattr(socket, "getaddrinfo", no_net)
    res = run_all()
    assert all_ok(res), json.dumps(res, indent=2, ensure_ascii=False)
    # the baseline file referenced by the stability eval must be committed
    assert res["score_stability"]["baseline_file_present"] is True


def test_committed_baselines_are_consistent_with_the_articles():
    """The baselines are script output; these checks pin the numbers the articles quote."""
    b = common.load_json(common.RESULTS / "score_stability_2026-11-01.json")
    rows = b["rows"]
    haiku = [r for r in rows if r["model"].startswith("claude-haiku")]
    sonnet = [r for r in rows if r["model"].startswith("claude-sonnet")]
    assert len(haiku) == len(sonnet) == 3, "models must stay separate rows"
    assert [r["default"]["range"] for r in haiku] == [4, 10, 14]
    assert [r["default"]["range"] for r in sonnet] == [4, 0, 0]
    assert all(r["t0"]["range"] == 0 for r in rows)
    assert sum(len(r["default"]["scores"]) + len(r["t0"]["scores"]) for r in rows) == 60
    p = common.load_json(common.RESULTS / "parse_robustness_2026-11-01.json")
    assert p["n"] == 12 and sum(p["text_path"].values()) == 12
    assert sum(p["tool_path"].values()) == 12 and p["text_path_stop_reason_max_tokens"] == 0
    f = common.load_json(common.RESULTS / "link_fetch_2026-11-01.json")
    assert sum(f["fetch_status"].values()) == f["candidates"] == 8
    assert sum(f["score_buckets"].values()) == f["fetch_status"]["ok"] == 7


def test_extract_shadow_check_baseline_matches_case_0005():
    d = common.load_json(common.RESULTS / "extract_shadow_check_2026-11-08.json")
    assert d["shadow_rows"] == d["scored"] == 21 and len(d["new_scores_sorted"]) == 21
    under = d["new_under_25 (deletion confirmed)"]
    mid = d["new_25_to_59 (would have been kept as rejected)"]
    top = d["new_60_plus (would have surfaced)"]
    assert (under, mid, top) == (19, 2, 0) and under + mid + top == 21
    assert sum(1 for s in d["new_scores_sorted"] if s < 25) == under
    assert round(sum(d["new_scores_sorted"]) / 21, 1) == d["new_mean"] == 13.3 and d["old_mean"] == 12.9
    t = d["cost"]["tokens_total"]
    assert (t["input"], t["output"], d["cost"]["calls"]) == (135605, 10524, 42) and d["cost"]["total_usd"] == 0.1882


def test_no_private_markers_in_public_eval_files():
    """Public repo: no keys, e-mails, Telegram ids/handles of people, profile text."""
    from _personal_needles import personal_needles
    bad = []
    for p in PUBLIC_FILES:
        text = p.read_text(encoding="utf-8")
        if re.search(r"sk-ant-[A-Za-z0-9_-]{10,}|\b\d{9,10}:AA[A-Za-z0-9_-]{30,}", text):
            bad.append((p.name, "secret"))
        if re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", text):
            bad.append((p.name, "email"))
        if re.search(r"(?i)\btg_id\b|\bchat_id\s*[=:]\s*\d{5,}", text):
            bad.append((p.name, "tg id"))
        for n in personal_needles():
            if n and n in text:
                bad.append((p.name, "personal needle"))
    assert not bad, bad


def test_no_habr_or_getmatch_content_in_fixtures():
    for p in (ROOT / "evals" / "synthetic").glob("*"):
        text = p.read_text(encoding="utf-8").lower()
        if p.name == "pages.json":  # the denylist URLs are the only allowed mentions
            text = re.sub(r"https?://[^\"\s]*(habr|getmatch)[^\"\s]*", "", text)
        assert "habr" not in text and "getmatch" not in text, p.name


def test_eval_modules_do_not_create_api_clients_on_import():
    import importlib, sys
    for mod in ("evals.score_stability_eval", "evals.parse_robustness_eval", "evals.link_fetch_eval"):
        sys.modules.pop(mod, None)
        importlib.import_module(mod)  # must not need an API key or DB


def test_score_prompt_v2_is_v1_minus_location_and_salary_deductions():
    from evals import score_prompt_eval as sp
    from job_hunter import llm
    from job_hunter.profile import example_profile

    p = example_profile()
    v1 = llm.build_score_system(p, tool=True)
    assert v1 == llm.build_score_system(p, tool=True, version="v1") and v1 == llm.build_score_system(p, tool=True)
    v2 = llm.build_score_system(p, tool=True, version="v2")
    for gone in ("LOCATION PRIORITY", "Location priority", "soft timezone-overlap", "salary unstated"):
        assert gone in v1 and gone not in v2
    assert "LOCATION IS NOT SCORED" in v2 and "does not state a salary is NOT a reason" in v2
    # hypotheses 3 and 4 untouched: seniority / thin-text wording identical
    assert "a senior role requiring proven tenure" in v2 and "REQUIREMENT SEVERITY" in v2
    assert "submit_score" in v2
    d = sp.prompt_diff()
    assert d.startswith("--- v1") and "-- LOCATION PRIORITY" in d


def test_score_prompt_paired_metrics():
    from evals import score_prompt_eval as sp

    rows = [{"label": "yes", "before": 45, "after": 55}, {"label": "no", "before": 55, "after": 40},
            {"label": "yes", "before": 70, "after": 72}]
    m = sp.paired_metrics(rows, 50)
    assert m["n"] == 3 and m["crossed_up_over_threshold"] == 1 and m["crossed_down_under_threshold"] == 1
    assert m["yes_at_or_above_T_before"] == 1 and m["yes_at_or_above_T_after"] == 2


def test_restore_candidates_selects_only_recent_system_rejects_in_band(conn):
    from datetime import datetime, timedelta, timezone
    from job_hunter import store
    from scripts.oneoff.restore_rejected_50_59 import restore_candidates

    def mk(n, score, days_old=1, state="rejected", actor="system", reason="score x < T"):
        i = store.insert_item(conn, raw_text=f"t{n}", source_channel="@c", source_link=f"https://t.me/c/{n}",
                              source_message_id=str(n))
        created = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
        conn.execute("UPDATE work_items SET state=%s, relevance_score=%s, created_at=%s WHERE id=%s",
                     (state, score, created, i))
        conn.execute("INSERT INTO state_transitions (item_id, from_state, to_state, kind, actor, reason, created_at) "
                     "VALUES (%s,'scored','rejected','deterministic',%s,%s,%s)", (i, actor, reason, created))
        conn.commit()
        return i

    ok = mk(1, 55)
    mk(2, 45); mk(3, 65); mk(4, 55, days_old=40); mk(5, 55, state="surfaced")
    mk(6, 55, reason="hard reject: salary below floor"); mk(7, 55, actor="user")
    assert restore_candidates(conn, 30) == [ok]


def test_score_prompt_confusion_and_movements():
    from evals import score_prompt_eval as sp

    rows = [{"cls": "relevant", "before": 45, "after": 55}, {"cls": "relevant", "before": 70, "after": 40},
            {"cls": "irrelevant", "before": 30, "after": 52}, {"cls": "irrelevant", "before": 60, "after": 61},
            {"cls": "excluded", "before": 55, "after": 40}]
    c1, c2 = sp.confusion(rows, "before", 50), sp.confusion(rows, "after", 50)
    assert (c1["relevant_above"], c1["irrelevant_above"]) == (1, 1) and c1["precision"] == 0.5
    assert (c2["relevant_above"], c2["irrelevant_above"]) == (1, 2)
    m = sp.movements(rows, 50)
    assert m["relevant"]["rose_above"] == 1 and m["relevant"]["fell_below"] == 1
    assert m["irrelevant"]["rose_above"] == 1 and m["excluded"]["fell_below"] == 1
