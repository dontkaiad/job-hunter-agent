"""evals/cost_probe.py -- what one pipeline step really costs, measured from API usage.

Runs extract, the Haiku score pass and the Sonnet corridor judge once per sampled
vacancy through the production code paths and reports, per step, the tokens and USD
taken from the `usage` block of the responses (including prompt-cache writes/reads).
COST.md quotes these numbers.

  offline -- the price arithmetic on fixed usage dicts (no API, no network).
  run     -- live: python -m evals.cost_probe run [--n 5] [--ids ...]
             Read-only DB; ~3 API calls per vacancy; costs a few cents.
             Default sample: the most recent ordinary posts (500-3000 chars).
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Optional

from evals.common import UsageMeter, price_for, report, save_result, usage_cost


def run_offline() -> Dict[str, Any]:
    u = {"input_tokens": 470, "output_tokens": 300,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1422}
    sonnet = usage_cost("claude-sonnet-4-6", u)
    haiku = usage_cost("claude-haiku-4-5", {"input_tokens": 1000, "output_tokens": 1000})
    return {
        "sonnet_cached_call_usd": round(sonnet, 6),
        "sonnet_math_ok": abs(sonnet - (470 * 3 + 300 * 15 + 1422 * 0.30) / 1e6) < 1e-12,
        "haiku_math_ok": abs(haiku - 0.006) < 1e-12,
        "unknown_model_is_unpriced_not_free": usage_cost("some-future-model", u) is None,
        "dated_variant_matches": price_for("claude-haiku-4-5-20251001") is not None,
    }


def run_live(n: int, ids: Optional[List[int]], save: Optional[str]) -> Dict[str, Any]:
    from evals.common import fetch_items, live_env
    from job_hunter import llm
    from job_hunter.schema_extract import from_dict

    cfg, profile, client, conn = live_env()
    if ids:
        rows = fetch_items(conn, ids)
    else:
        rows = conn.execute(
            "SELECT id, relevance_score, raw_text, extracted_json, source_link, source_channel "
            "FROM work_items WHERE state <> 'extracted' AND length(raw_text) BETWEEN 500 AND 3000 "
            "ORDER BY id DESC LIMIT %s", (n,)).fetchall()
    steps: Dict[str, List[Dict[str, Any]]] = {"extract": [], "score_haiku": [], "score_sonnet": []}
    for r in rows:
        ex = from_dict(json.loads(r["extracted_json"]))
        for name, fn in (
            ("extract", lambda: llm.llm_extract(client, r["raw_text"], r.get("source_channel") or "x",
                                                r.get("source_link"), model=cfg.cheap_model)),
            ("score_haiku", lambda: llm.llm_score(client, ex, r["raw_text"], model=cfg.cheap_model, profile=profile)),
            ("score_sonnet", lambda: llm.llm_score(client, ex, r["raw_text"], model=cfg.judge_model, profile=profile)),
        ):
            before = len(client.meter.calls)
            try:
                fn()
            except llm.InsufficientContent:
                pass
            for model, usage in client.meter.calls[before:]:
                steps[name].append({"model": model, **usage, "usd": usage_cost(model, usage)})
    summary = {}
    for name, calls in steps.items():
        if not calls:
            continue
        summary[name] = {
            "calls": len(calls),
            "mean_usd": round(sum(c["usd"] for c in calls) / len(calls), 5),
            "mean_input_tokens": round(sum(c["input_tokens"] for c in calls) / len(calls)),
            "mean_cache_read_tokens": round(sum(c["cache_read_input_tokens"] for c in calls) / len(calls)),
            "mean_cache_write_tokens": round(sum(c["cache_creation_input_tokens"] for c in calls) / len(calls)),
            "mean_output_tokens": round(sum(c["output_tokens"] for c in calls) / len(calls)),
            "per_call_usd": [round(c["usd"], 5) for c in calls],
        }
    out = {"vacancies": len(rows), "steps": summary, "cost": client.meter.report()}
    if save:
        save_result(save, out)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("offline")
    r = sub.add_parser("run")
    r.add_argument("--n", type=int, default=5)
    r.add_argument("--ids", type=int, nargs="*", default=None)
    r.add_argument("--save", default=None)
    a = ap.parse_args(argv)
    report("cost probe", run_offline() if a.cmd == "offline" else run_live(a.n, a.ids, a.save))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
