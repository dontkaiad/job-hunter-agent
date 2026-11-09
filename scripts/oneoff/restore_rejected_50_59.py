"""ONE-OFF (SPENDS a few cents, guarded): put the vacancies rejected at 50-59 back through the gate.

The surface threshold moved 60 -> 50 (docs/CASES/0006). Vacancies the system rejected at 50-59 under
the old threshold are returned to `scored` and advanced by the normal pipeline step, so the geo
guard decides them exactly as it would in a harvest: INELIGIBLE stays rejected (shown in the
dashboard's geo-cut section), everything else becomes `surfaced`. Only vacancies seen in the last
--days days (posted_at, else created_at) are touched. Never touched: hard rejects (salary / location
guards), anything a human rejected, anything the geo guard already judged.

    python scripts/oneoff/restore_rejected_50_59.py --max-usd 0.03            # estimate, no change
    python scripts/oneoff/restore_rejected_50_59.py --max-usd 0.03 --confirm  # do it
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

CANDIDATES_SQL = """
SELECT w.id FROM work_items w
WHERE w.state = 'rejected' AND w.relevance_score >= %s AND w.relevance_score < %s
  AND w.geo_verdict IS NULL
  AND COALESCE(w.posted_at, w.created_at) >= %s
  AND NOT EXISTS (SELECT 1 FROM state_transitions t WHERE t.item_id = w.id AND t.to_state = 'rejected'
                  AND (t.reason LIKE 'hard reject%%' OR t.actor <> 'system'))
ORDER BY w.id
"""


def restore_candidates(conn, days: int, lo: float = 50.0, hi: float = 60.0) -> List[int]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [r["id"] for r in conn.execute(CANDIDATES_SQL, (lo, hi, cutoff)).fetchall()]


def main(argv=None) -> int:
    from evals.common import add_budget_args, est_call_usd, est_tokens, gate, live_ctx, make_client, print_usage_summary, report
    from job_hunter import geo, pipeline, store
    from job_hunter.bot import build_deps
    import json

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    add_budget_args(ap)
    a = ap.parse_args(argv)

    cfg, _profile, ro = live_ctx()
    ids = restore_candidates(ro, a.days, lo=cfg.surface_threshold, hi=60.0)
    rows = [store.get_item(ro, i) for i in ids]
    tool_tokens = est_tokens(json.dumps(geo.GEO_TOOL))
    est = sum(est_call_usd(cfg.cheap_model, geo.GEO_SYSTEM, geo.build_geo_prompt(r.raw_text, r.fetched_text),
                           150, extra_tokens=tool_tokens) for r in rows)
    gate("restore rejected 50-59", est, a.max_usd, a.confirm,
         f"{len(ids)} vacancies rejected at {cfg.surface_threshold}-59 in the last {a.days} days (geo check only).")
    client = make_client(cfg, "restore_rejected")
    conn = store.connect(cfg.database_url)
    store.init_db(conn)
    deps = build_deps(cfg)
    deps.llm_client = client                      # the guarded, metered client
    deps.score_stats = pipeline.ScoreRunStats()
    outcome: Dict[str, Any] = {}
    for i in ids:
        item = store.get_item(conn, i)
        if item is None or item.state != "rejected":
            continue
        store.update_state(conn, i, "scored", from_state="rejected", kind="deterministic", actor="system",
                           channel="script", reason=f"restore: surface threshold moved to {cfg.surface_threshold}")
        res = pipeline.advance_by_id(conn, i, deps=deps)
        key = res.to_state or res.status
        outcome[key] = outcome.get(key, 0) + 1
    s = deps.score_stats
    print_usage_summary(client.meter)
    report("restore rejected 50-59", {"candidates": len(ids), "result": outcome, "geo_cut": s.geo_cut,
                                      "geo_unknown": s.geo_unknown, "geo_failed": s.geo_failed,
                                      "cost": client.meter.report()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
