"""evals/parse_robustness_eval.py -- text JSON parsing vs forced tool use (submit_score).

Question: how often does a score reply fail to parse, and of what kind?
Method: classify every reply into a break type; compare the legacy TEXT path (free
text -> regex JSON extraction) with the production TOOL path (forced tool_choice).

  offline -- (a) the four break types on synthetic replies: which are salvageable by
             the text parser; (b) the saved, truncated prose refusals of the
             2026-11-01 incident all raise on the text path; (c) the tool path
             parks insufficient_content / null score instead of failing.
  run     -- live: python -m evals.parse_robustness_eval run --ids 11009 11063 ...
             Replays the given vacancies through both paths (read-only DB, ~2 API
             calls per vacancy, Haiku, ~$0.03 for 12). Prints counts by break type.

Caveat: the tool path cannot emit prose by construction, so "0 parse failures"
there is expected -- it says nothing about whether the SCORES are right.
"""
from __future__ import annotations

import argparse
import collections
import json
from typing import Any, Dict, List, Optional

from evals.common import SYNTHETIC, load_json, report


def classify_reply(text: str, stop_reason: Optional[str] = None) -> str:
    """Break type of a text-path reply: OK | empty | truncated | markdown-wrapper |
    prose-no-json | invalid-escape | other."""
    from job_hunter import llm

    t = (text or "").strip()
    if not t:
        return "empty"
    try:
        llm.parse_score_response(t)
        return "OK"
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
    if stop_reason == "max_tokens" or t.count("{") > t.count("}"):
        return "truncated"
    if "```" in t:
        return "markdown-wrapper"
    if "{" not in t:
        return "prose-no-json"
    if "escape" in err or "control character" in err:
        return "invalid-escape"
    return "other"


# Synthetic replies: one per break type (NOT real model output).
SYNTHETIC_REPLIES = {
    "ok": '{"relevance_score": 61, "Обоснование": "x"}',
    "markdown-wrapper": '```json\n{"relevance_score": 61, "Обоснование": "x"}\n```',
    "truncated": '{"relevance_score": 61, "Обоснование": "Сильный фит — 61/100\\n✅ Роль про',
    "prose-no-json": "I cannot access external URLs, please paste the posting text.",
    "invalid-escape": '{"relevance_score": 61, "Обоснование": "path C:\\users"}',
}


def run_offline() -> Dict[str, Any]:
    from job_hunter import llm

    kinds = {k: classify_reply(v) for k, v in SYNTHETIC_REPLIES.items()}
    kinds_ok = (kinds["ok"] == "OK" and kinds["truncated"] == "truncated"
                and kinds["prose-no-json"] == "prose-no-json"
                and kinds["invalid-escape"] == "invalid-escape")
    # a ``` wrapper is salvaged by the parser's fence pre-processing
    fence_salvaged = kinds["markdown-wrapper"] == "OK"

    fx = load_json(SYNTHETIC / "score_prose_refusals.json")
    incident = [classify_reply(c["head"] + " ... " + c["tail"]) for c in fx["refusals"]]

    cases = [
        {"relevance_score": None, "insufficient_content": True, "reasoning": "only a link"},
        {"relevance_score": None, "insufficient_content": False, "reasoning": ""},
        {"relevance_score": 10, "insufficient_content": True, "reasoning": "no content"},
    ]
    parked = 0
    for c in cases:
        try:
            llm.parse_score_tool_input(c)
        except llm.InsufficientContent:
            parked += 1
    return {
        "synthetic_break_types": kinds,
        "break_types_classified_ok": kinds_ok,
        "markdown_fence_salvaged_by_text_parser": fence_salvaged,
        "incident_prose_refusals": len(incident),
        "incident_prose_refusals_all_fail_text_parser": all(k == "prose-no-json" for k in incident),
        "tool_path_parked_not_failed": f"{parked}/{len(cases)}",
        "tool_path_parks_all_insufficient": parked == len(cases),
    }


def run_live(ids: List[int]) -> Dict[str, Any]:
    from evals.common import fetch_items, live_env
    from job_hunter import llm, scoring
    from job_hunter.schema_extract import from_dict

    cfg, profile, client, conn = live_env()
    text_types: collections.Counter = collections.Counter()
    tool_outcomes: collections.Counter = collections.Counter()
    in_chars: Dict[str, List[int]] = {"OK": [], "failed": []}
    max_tokens_hits = 0
    rows = fetch_items(conn, ids)
    for r in rows:
        ex = from_dict(json.loads(r["extracted_json"]))
        # legacy text path
        system = llm.build_score_system(profile)
        resp = client._client.messages.create(
            model=cfg.cheap_model, max_tokens=llm.SCORE_MAX_TOKENS, system=system,
            messages=[{"role": "user", "content": llm.build_score_prompt(ex, r["raw_text"] or "")}],
        )
        text = "".join(getattr(b, "text", "") or "" for b in resp.content)
        kind = classify_reply(text, resp.stop_reason)
        text_types[kind] += 1
        in_chars["OK" if kind == "OK" else "failed"].append(len(r["raw_text"] or ""))
        max_tokens_hits += resp.stop_reason == "max_tokens"
        # production path (gate -> forced tool)
        if scoring.is_link_only(r["raw_text"], cfg.link_only_min_chars):
            tool_outcomes["link_only (no LLM call)"] += 1
            continue
        try:
            llm.llm_score(client, ex, r["raw_text"] or "", model=cfg.cheap_model, profile=profile)
            tool_outcomes["scored"] += 1
        except llm.InsufficientContent:
            tool_outcomes["insufficient_content"] += 1
        except Exception as exc:  # noqa: BLE001
            tool_outcomes[f"FAILED {type(exc).__name__}"] += 1
    return {
        "n": len(rows),
        "model": cfg.cheap_model,
        "text_path": dict(text_types),
        "text_path_stop_reason_max_tokens": max_tokens_hits,
        "text_path_input_chars": {k: sorted(v) for k, v in in_chars.items()},
        "tool_path": dict(tool_outcomes),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("offline")
    r = sub.add_parser("run")
    r.add_argument("--ids", type=int, nargs="+", required=True)
    a = ap.parse_args(argv)
    report("parse robustness", run_offline() if a.cmd == "offline" else run_live(a.ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
