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
