"""evals/cost_model.py -- weekly $ before/after for prompt caching and Message Batches.

  python -m evals.cost_model scenarios --items-per-week N [--corridor-share 0.25]
      Price N postings/week through the extract -> Haiku score -> (corridor) Sonnet
      judge flow under four scenarios. Per-call token sizes are the MEASURED ones in
      COST.md (cost_probe, 2026-11-01); the volume and the corridor share are inputs.
  python -m evals.cost_model measured [--days 7]
      Read the real last-N-days tokens from the `llm_usage` table (needs DATABASE_URL;
      only fills once the usage logging has been running) and price them as-is.

Scenarios: baseline | haiku-cache (score prefix padded past Haiku's 4096 floor) |
batch (every step at 50%, caches best-effort) | both.
"""
from __future__ import annotations

import argparse
import os
from typing import Any, Dict

from evals.common import PRICES, report

# Measured per call (COST.md "Measured per-call cost", n=5). `sys` = cacheable prefix
# (tools + system), `rest` = uncached remainder of input.
EXTRACT = {"model": "claude-haiku-4-5", "sys": 1254, "rest": 2107 - 1254, "out": 203}
HAIKU_SCORE = {"model": "claude-haiku-4-5", "sys": 3752, "rest": 4536 - 3752, "out": 414}
SONNET_JUDGE = {"model": "claude-sonnet-4-6", "sys": 3384, "rest": 1108, "out": 435}
HAIKU_FLOOR = 4096


def call_usd(step: Dict[str, Any], *, cached: bool, hit: float, pad: int = 0,
             batch: bool = False) -> float:
    """USD for one call. cached: prefix carries cache_control and is >= the floor.
    hit: share of calls that READ the cache (rest pay a write). batch: 50% off."""
    p = PRICES[step["model"]]
    sys_tokens = step["sys"] + pad
    if cached:
        per_call = sys_tokens * (hit * p["cache_read"] + (1 - hit) * p["cache_write_5m"])
    else:
        per_call = sys_tokens * p["input"]
    usd = (per_call + step["rest"] * p["input"] + step["out"] * p["output"]) / 1e6
    return usd * (0.5 if batch else 1.0)


def scenarios(items: int, corridor: float, sync_hit: float, batch_hit: float) -> Dict[str, Any]:
    pad = HAIKU_FLOOR - HAIKU_SCORE["sys"]          # tokens to add so Haiku score caches
    out = {}
    for name, (cache_haiku, batch) in {
        "baseline": (False, False), "haiku-cache": (True, False),
        "batch": (False, True), "both": (True, True),
    }.items():
        hit = batch_hit if batch else sync_hit
        extract = call_usd(EXTRACT, cached=False, hit=0, batch=batch)
        score = call_usd(HAIKU_SCORE, cached=cache_haiku, hit=hit,
                         pad=pad if cache_haiku else 0, batch=batch)
        judge = call_usd(SONNET_JUDGE, cached=True, hit=hit, batch=batch)
        per_item = extract + score + corridor * judge
        out[name] = {"usd_per_item": round(per_item, 5), "usd_per_week": round(per_item * items, 2)}
    base = out["baseline"]["usd_per_week"]
    for v in out.values():
        v["saving_pct"] = round(100 * (1 - v["usd_per_week"] / base), 1) if base else 0.0
    return {
        "assumptions": {
            "items_per_week": items, "corridor_share": corridor,
            "cache_hit_sync": sync_hit, "cache_hit_batch": batch_hit,
            "haiku_score_prefix_tokens": HAIKU_SCORE["sys"], "pad_tokens_needed": pad,
            "note": "token sizes measured (COST.md); volume/corridor/hit-rates are inputs",
        },
        "scenarios": out,
    }


def measured(days: int) -> Dict[str, Any]:
    import psycopg
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = psycopg.connect(os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on")
    rows = conn.execute(
        "SELECT step, model, mode, count(*), sum(input_tokens), sum(cache_write_tokens),"
        " sum(cache_read_tokens), sum(output_tokens) FROM llm_usage WHERE ts >= %s"
        " GROUP BY step, model, mode ORDER BY step, model", (since,)).fetchall()
    out, total = [], 0.0
    for step, model, mode, n, i, w, r, o in rows:
        p = PRICES.get(model)
        usd = None if p is None else (i * p["input"] + w * p["cache_write_5m"]
                                      + r * p["cache_read"] + o * p["output"]) / 1e6
        if usd is not None:
            usd *= 0.5 if mode == "batch" else 1.0
            total += usd
        out.append({"step": step, "model": model, "mode": mode, "calls": n, "input": int(i),
                    "cache_write": int(w), "cache_read": int(r), "output": int(o),
                    "usd": None if usd is None else round(usd, 4)})
    return {"days": days, "since": since, "total_usd": round(total, 4), "rows": out}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scenarios")
    s.add_argument("--items-per-week", type=int, required=True)
    s.add_argument("--corridor-share", type=float, default=0.25)
    s.add_argument("--cache-hit-sync", type=float, default=0.95)
    s.add_argument("--cache-hit-batch", type=float, default=0.5)
    m = sub.add_parser("measured")
    m.add_argument("--days", type=int, default=7)
    a = ap.parse_args(argv)
    if a.cmd == "scenarios":
        report("cost scenarios", scenarios(a.items_per_week, a.corridor_share,
                                           a.cache_hit_sync, a.cache_hit_batch))
    else:
        report("measured usage", measured(a.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
