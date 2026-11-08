"""ONE-OFF (SPENDS MONEY, guarded): redo the extraction that failed over to heuristics.

2026-11-02..05 the account had no credits and extract silently fell back to the heuristic
extractor. Two steps, run separately, each with its own --max-usd and --confirm
(without --confirm only the estimate is printed and nothing is spent):

  shadow-check       READ-ONLY. For the below-the-cut vacancies of that cohort that are
                     still held in eval_shadow (text kept), re-extract with the model and
                     score on the current system. Prints aggregates: how many of them would
                     have scored >=25 and >=60. Writes nothing.
  reextract-unscored WRITES. For the unscored vacancies still carrying a fallback
                     extraction, replace extracted_json with the model's extraction
                     (extract_mode='llm'). Scores nothing: the normal harvest scores them.

    python scripts/oneoff/reextract_fallback.py shadow-check --max-usd 0.30
    python scripts/oneoff/reextract_fallback.py shadow-check --max-usd 0.30 --confirm
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from typing import Any, Dict, List

from evals.common import (
    BudgetExceeded, add_budget_args, est_call_usd, est_score_call_usd, gate, live_ctx, make_client,
    print_usage_summary, report,
)
from evals.golden_eval import score_current
from job_hunter import llm, store
from job_hunter.extract import reconcile as reconcile_extract
from job_hunter.schema_extract import from_dict, serialize

JUDGE_SHARE = 0.15


def cohort_id_range(conn, d_from: str, d_to: str):
    """Id range of the vacancies ingested d_from..d_to (inclusive dates), from survivors."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM work_items WHERE substr(created_at,1,10) >= %s AND substr(created_at,1,10) <= %s",
        (d_from, d_to)).fetchall()]
    return (min(ids), max(ids)) if ids else (None, None)


def new_extraction(client, cfg, raw, channel, link):
    ex = llm.llm_extract(client, raw, channel or "", link, model=cfg.cheap_model)
    ex = reconcile_extract(ex, raw, channel or "", link)
    ex.source_channel = channel or ""
    if link:
        ex.source_link = link
    return ex


def estimate_extract(cfg, raw, channel, link) -> float:
    return est_call_usd(cfg.cheap_model, llm.EXTRACT_SYSTEM,
                        llm.build_extract_prompt(raw, channel or "", link), 203)


def run_shadow_check(a) -> Dict[str, Any]:
    cfg, profile, conn = live_ctx()
    lo, hi = cohort_id_range(conn, a.from_date, a.to_date)
    rows = conn.execute(
        "SELECT orig_item_id, raw_text, fetched_text, source_channel, source_link, score "
        "FROM eval_shadow WHERE kind = 'score' AND orig_item_id BETWEEN %s AND %s ORDER BY orig_item_id",
        (lo, hi)).fetchall()
    est = 0.0
    for r in rows:
        ex0 = from_dict({"title": "x"})
        est += estimate_extract(cfg, r["raw_text"], r["source_channel"], r["source_link"])
        est += est_score_call_usd(cfg.cheap_model, profile, ex0, r["raw_text"], r["fetched_text"])
        est += JUDGE_SHARE * est_score_call_usd(cfg.judge_model, profile, ex0, r["raw_text"], r["fetched_text"])
    gate("reextract shadow-check", est, a.max_usd, a.confirm,
         f"{len(rows)} shadow rows with ids {lo}..{hi}; extract + score on the current system.")
    client = make_client(cfg, "reextract_shadow_check")
    new_scores: List[float] = []
    outcome: collections.Counter = collections.Counter()
    old_scores: List[float] = []
    for r in rows:
        try:
            ex = new_extraction(client, cfg, r["raw_text"], r["source_channel"], r["source_link"])
            s, _judged = score_current(client, ex, r["raw_text"], r["fetched_text"], cfg, profile)
        except BudgetExceeded:
            outcome["stopped (budget)"] += 1
            break
        except llm.InsufficientContent:
            outcome["insufficient content"] += 1
            continue
        except Exception as exc:  # noqa: BLE001
            outcome[f"error {type(exc).__name__}"] += 1
            continue
        old_scores.append(r["score"])
        new_scores.append(s)
        outcome["scored"] += 1
    print_usage_summary(client.meter)
    n = len(new_scores)
    return {
        "shadow_rows": len(rows), "outcomes": dict(outcome),
        "scored": n,
        "old_mean": round(sum(old_scores) / n, 1) if n else None,
        "new_mean": round(sum(new_scores) / n, 1) if n else None,
        "new_under_25 (deletion confirmed)": sum(1 for s in new_scores if s < 25),
        "new_25_to_59 (would have been kept as rejected)": sum(1 for s in new_scores if 25 <= s < 60),
        "new_60_plus (would have surfaced)": sum(1 for s in new_scores if s >= 60),
        "new_scores_sorted": sorted(new_scores),
        "cost": client.meter.report(),
    }


def run_reextract_unscored(a) -> Dict[str, Any]:
    cfg, profile, ro = live_ctx()
    rows = ro.execute(
        "SELECT id, raw_text, source_channel, source_link FROM work_items WHERE state = 'extracted' "
        "AND extract_mode = 'heuristic:fallback' AND NOT score_failed AND NOT extract_failed ORDER BY id").fetchall()
    est = sum(estimate_extract(cfg, r["raw_text"], r["source_channel"], r["source_link"]) for r in rows)
    gate("reextract unscored", est, a.max_usd, a.confirm,
         f"{len(rows)} unscored vacancies with a fallback extraction; extract only, scoring is left to the harvest.")
    client = make_client(cfg, "reextract_unscored")
    rw = store.connect(cfg.database_url)
    done, outcome = 0, collections.Counter()
    for r in rows:
        try:
            ex = new_extraction(client, cfg, r["raw_text"], r["source_channel"], r["source_link"])
        except BudgetExceeded:
            outcome["stopped (budget)"] += 1
            break
        except Exception as exc:  # noqa: BLE001 - leave the item as it was
            outcome[f"error {type(exc).__name__}"] += 1
            continue
        store.replace_extraction(rw, r["id"], serialize(ex), "llm",
                                 "re-extract (llm+reconcile) of a failed-over heuristic extraction")
        done += 1
    print_usage_summary(client.meter)
    return {"candidates": len(rows), "re_extracted": done, "left_untouched": len(rows) - done,
            "outcomes": dict(outcome), "cost": client.meter.report()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="step", required=True)
    s1 = sub.add_parser("shadow-check")
    s1.add_argument("--from-date", default="2026-11-02")
    s1.add_argument("--to-date", default="2026-11-05")
    add_budget_args(s1)
    s2 = sub.add_parser("reextract-unscored")
    add_budget_args(s2)
    a = ap.parse_args(argv)
    report(f"reextract: {a.step}", run_shadow_check(a) if a.step == "shadow-check" else run_reextract_unscored(a))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
