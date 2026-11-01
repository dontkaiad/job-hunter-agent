"""evals/score_stability_eval.py -- score reproducibility: temperature default (1.0) vs 0.

Question: does the same posting get the same relevance score on repeated calls?
Method: run the PRODUCTION scorer (llm.llm_score: forced tool, same prompt) N times
per vacancy under two conditions -- API-default temperature and temperature=0 -- on
both scoring models, and report min / max / range per cell. No DB writes.

  offline -- harness check on a scripted fake client (no API, no network): spread
             maths + a regression guard that production scoring pins temperature=0.
  run     -- live: python -m evals.score_stability_eval run --ids 10671 11097 11197
             Reads the vacancies from the DB (read-only), calls the API
             (~60 calls for 3 vacancies x 5 runs x 2 models x 2 conditions, ~$0.3).
             Prints aggregates only; --save NAME writes them to evals/results/.

Measures REPEATABILITY, not correctness: a stable wrong score is still wrong.
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Optional

from evals.common import load_json, RESULTS, report, save_result, spread


def measure(score_once, conditions, models, items, runs: int) -> List[Dict[str, Any]]:
    """score_once(model, item, temperature) -> int. Pure orchestration."""
    rows = []
    for model in models:
        for item in items:
            row: Dict[str, Any] = {"model": model, "id": item["id"],
                                   "stored": item.get("relevance_score")}
            for label, temp in conditions:
                scores = []
                for _ in range(runs):
                    try:
                        scores.append(score_once(model, item, temp))
                    except Exception as exc:  # noqa: BLE001
                        scores.append(type(exc).__name__)
                row[label] = {"scores": scores, **spread(scores)}
            rows.append(row)
    return rows


def run_offline() -> Dict[str, Any]:
    from job_hunter import llm
    from job_hunter.schema_extract import ExtractResult

    # 1) harness: scripted sequences reproduce the spread maths exactly
    seqs = {"noisy": [18, 28, 28, 15, 28], "stable": [62] * 5}
    it = iter([s for k in ("noisy", "stable") for s in seqs[k]])
    rows = measure(lambda m, i, t: next(it), [("x", None)], ["m"],
                   [{"id": "noisy"}, {"id": "stable"}], 5)
    ok_math = (rows[0]["x"]["range"] == 13 and rows[1]["x"]["range"] == 0)

    # 2) guard: production scoring pins temperature 0 on both the tool and text path
    seen: List[Optional[float]] = []

    class Fake:
        def complete_tool(self, *a, temperature=None, **k):
            seen.append(temperature)
            return {"relevance_score": 50, "insufficient_content": False, "reasoning": "x"}

    ex = ExtractResult(title="t", source_channel="c")
    llm.llm_score(Fake(), ex, "text", model="claude-haiku-4-5")
    llm.llm_score(Fake(), ex, "text", model="claude-sonnet-4-6")
    return {
        "spread_maths_ok": ok_math,
        "production_temperature": llm.SCORE_TEMPERATURE,
        "temperature_sent_to_api": seen,
        "temperature_pinned_ok": llm.SCORE_TEMPERATURE == 0 and seen == [0.0, 0.0],
        "baseline_file_present": (RESULTS / "score_stability_2026-11-01.json").exists(),
    }


def run_live(ids: List[int], runs: int, save: Optional[str]) -> Dict[str, Any]:
    from evals.common import fetch_items, live_env
    from job_hunter import llm
    from job_hunter.schema_extract import from_dict

    cfg, profile, client, conn = live_env()
    items = fetch_items(conn, ids)

    def score_once(model, item, temp):
        _set_temp(client, temp)
        return llm.llm_score(client, from_dict(json.loads(item["extracted_json"])),
                             item["raw_text"], model=model, profile=profile)["score"]

    rows = measure(score_once, [("default", None), ("t0", 0)],
                   [cfg.cheap_model, cfg.judge_model], items, runs)
    out = {"runs_per_cell": runs, "vacancies": len(items), "rows": rows,
           "cost": client.meter.report()}
    if save:
        save_result(save, out)
    return out


def _set_temp(client, temp):
    """Make every request carry exactly `temp` (None = the API default). Production
    pins 0 via llm.SCORE_TEMPERATURE, so the baseline must strip/override it."""
    base = getattr(client, "_orig_create", None) or client._client.messages.create
    client._orig_create = base

    def create(**kw):
        kw.pop("temperature", None)
        if temp is not None:
            kw["temperature"] = temp
        return base(**kw)

    client._client.messages.create = create


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("offline")
    r = sub.add_parser("run")
    r.add_argument("--ids", type=int, nargs="+", required=True)
    r.add_argument("--runs", type=int, default=5)
    r.add_argument("--save", default=None, help="file name under evals/results/")
    a = ap.parse_args(argv)
    if a.cmd == "offline":
        report("score stability (offline)", run_offline())
        return 0
    report("score stability (live)", run_live(a.ids, a.runs, a.save))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
