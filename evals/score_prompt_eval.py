"""evals/score_prompt_eval.py -- one paired iteration of the score prompt, on DEV only.

v1 = production prompt; v2 = candidate (job_hunter/llm.py, SCORE_PROMPT_VERSIONS): location /
relocation / visa / timezone are not scored (the geo guard decides them) and an unstated salary is
not a reason to lower the score. Hypotheses 3 and 4 are deliberately NOT in v2, so the effect of
1 + 2 is isolated.

  diff     -- NO API/DB: unified diff v1 -> v2 rendered with the generic example profile.
  estimate -- NO API: exact-token $ estimate to score the dev vacancies under v1 / v2.
  run      -- live (SPENDS): scores the dev vacancies under --versions (default v2) with the production
              route (Haiku, Sonnet judge in the corridor) -> eval_prompt_scores; --max-usd required,
              estimate first, --confirm to spend. Resumable: already scored (item, version) pairs skip.
  report   -- NO API: paired v1 -> v2 on the same vacancies (needs both versions stored).
The held-out split is never touched here.
"""
from __future__ import annotations

import argparse
import collections
import difflib
import json
from typing import Any, Dict, List, Optional

from evals.common import (
    BudgetExceeded, add_budget_args, db_ctx, est_score_call_usd, gate, live_ctx, make_client,
    print_usage_summary, report,
)
from evals.geo_facts_eval import split_of
from job_hunter import golden

JUDGE_SHARES = (0.15, 0.30, 1.0)
# The stored "before" scores (eval_frame) were produced with the corridor [50, 70]; the iteration
# runs v2 with the SAME corridor so the only difference between before and after is the prompt.
PAIRED_CORRIDOR = (50, 70)


def paired_cfg(cfg):
    import dataclasses

    return dataclasses.replace(cfg, score_corridor_lo=PAIRED_CORRIDOR[0], score_corridor_hi=PAIRED_CORRIDOR[1])


def prompt_diff() -> str:
    from job_hunter import llm
    from job_hunter.profile import example_profile

    p = example_profile()
    a = llm.build_score_system(p, tool=True, version="v1").splitlines()
    b = llm.build_score_system(p, tool=True, version="v2").splitlines()
    return "\n".join(difflib.unified_diff(a, b, "v1", "v2", lineterm="", n=0))


def dev_items(conn) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT DISTINCT ON (s.item_id) s.item_id, s.held_out, l.label, "
        "COALESCE(w.raw_text, v.raw_text) AS raw_text, COALESCE(w.fetched_text, v.fetched_text) AS fetched_text, "
        "COALESCE(w.extracted_json, v.extracted_json) AS extracted_json "
        "FROM eval_sample s JOIN eval_labels l ON l.sample_id = s.id "
        "LEFT JOIN work_items w ON w.id = s.item_id LEFT JOIN eval_vacancies v ON v.item_id = s.item_id "
        "WHERE NOT s.is_control AND COALESCE(w.raw_text, v.raw_text) IS NOT NULL "
        "ORDER BY s.item_id, s.wave").fetchall()
    return [dict(r) for r in rows if split_of(r) == "dev"]


def estimate_usd(cfg, profile, items, version: str, judge_share: float) -> float:
    from job_hunter.schema_extract import from_dict

    usd = 0.0
    for r in items:
        ex = from_dict(json.loads(r["extracted_json"]))
        usd += est_score_call_usd(cfg.cheap_model, profile, ex, r["raw_text"], r["fetched_text"], version=version)
        usd += judge_share * est_score_call_usd(cfg.judge_model, profile, ex, r["raw_text"], r["fetched_text"],
                                                version=version)
    return usd


def run_estimate() -> Dict[str, Any]:
    from job_hunter.profile import load_profile

    cfg, conn = db_ctx()
    cfg = paired_cfg(cfg)
    profile = load_profile()
    items = dev_items(conn)
    ids = [r["item_id"] for r in items]
    frame = {r["item_id"]: r for r in conn.execute(
        "SELECT item_id, status, current_score FROM eval_frame WHERE item_id = ANY(%s)", (ids,)).fetchall()}
    lo, hi = cfg.score_corridor_lo, cfg.score_corridor_hi
    in_corridor = sum(1 for i in ids if frame.get(i) and frame[i]["status"] == "scored"
                      and lo <= frame[i]["current_score"] <= hi)
    out: Dict[str, Any] = {
        "dev_vacancies": len(items), "corridor": [lo, hi],
        "judge_share_proxy": round(in_corridor / len(items), 2),
        "judge_share_proxy_note": "dev items whose CURRENT final score lies in the corridor; the real routing "
                                  "is by the Haiku score, which is not stored",
    }
    for v in ("v1", "v2"):
        out[f"usd_{v}"] = {f"judge_share_{int(s * 100)}pct": round(estimate_usd(cfg, profile, items, v, s), 3)
                           for s in JUDGE_SHARES}
    out["notes"] = ("Upper-bound style: no prompt-cache credit, offline token estimate x1.085 calibrated on measured "
                    "calls (evals/common.py). Haiku = every vacancy, Sonnet = the share routed to the judge.")
    return out


def run_score(versions: List[str], max_usd: float, confirm: bool, judge_share: float) -> Dict[str, Any]:
    from evals.golden_eval import score_current
    from job_hunter import llm, store
    from job_hunter.clock import now_iso
    from job_hunter.schema_extract import from_dict

    cfg, profile, ro = live_ctx()
    cfg = paired_cfg(cfg)
    items = dev_items(ro)
    have = {(r["item_id"], r["version"]) for r in ro.execute("SELECT item_id, version FROM eval_prompt_scores").fetchall()}
    todo = [(r, v) for v in versions for r in items if (r["item_id"], v) not in have]
    est = 0.0
    for r, v in todo:
        ex = from_dict(json.loads(r["extracted_json"]))
        est += est_score_call_usd(cfg.cheap_model, profile, ex, r["raw_text"], r["fetched_text"], version=v)
        est += judge_share * est_score_call_usd(cfg.judge_model, profile, ex, r["raw_text"], r["fetched_text"], version=v)
    gate("score prompt iteration", est, max_usd, confirm,
         f"{len(todo)} scorings (dev only, versions {versions}); judge share assumed {judge_share:.0%}.")
    client = make_client(cfg, "score_prompt_eval")
    rw = store.connect(cfg.database_url)
    store.init_db(rw)
    outcome = collections.Counter()
    for r, v in todo:
        client.meter.thread_cost_reset()
        status, score, judge = "scored", None, False
        try:
            score, judge = score_current(client, from_dict(json.loads(r["extracted_json"])), r["raw_text"],
                                         r["fetched_text"], cfg, profile, prompt_version=v)
        except BudgetExceeded:
            outcome["stopped (budget)"] += 1
            break
        except llm.InsufficientContent:
            status = "insufficient"
        except Exception as exc:  # noqa: BLE001
            outcome[f"error {type(exc).__name__}"] += 1
            continue
        rw.execute("INSERT INTO eval_prompt_scores (item_id, version, status, score, judge_used, cost_usd, scored_at) "
                   "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (item_id, version) DO NOTHING",
                   (r["item_id"], v, status, score, judge, client.meter.thread_cost(), now_iso()))
        rw.commit()
        outcome[f"{v} {status}"] += 1
    print_usage_summary(client.meter)
    return {"to_score": len(todo), "outcomes": dict(outcome), "cost": client.meter.report()}


def paired_metrics(rows: List[Dict[str, Any]], threshold: float) -> Dict[str, Any]:
    """rows: {label, before, after} (scores, both present). PURE. Aggregates only."""
    n = len(rows)
    if not n:
        return {"n": 0}
    d = [r["after"] - r["before"] for r in rows]
    cross_up = sum(1 for r in rows if r["before"] < threshold <= r["after"])
    cross_dn = sum(1 for r in rows if r["after"] < threshold <= r["before"])
    out: Dict[str, Any] = {
        "n": n, "mean_delta": round(sum(d) / n, 2),
        "abs_delta_ge_10": sum(1 for x in d if abs(x) >= 10),
        "crossed_up_over_threshold": cross_up, "crossed_down_under_threshold": cross_dn,
    }
    for lab, name in (("yes", "yes"), ("no", "no")):
        g = [r for r in rows if r["label"] == lab]
        if g:
            out[f"{name}_n"] = len(g)
            out[f"{name}_mean_delta"] = round(sum(r["after"] - r["before"] for r in g) / len(g), 2)
            out[f"{name}_at_or_above_T_before"] = sum(1 for r in g if r["before"] >= threshold)
            out[f"{name}_at_or_above_T_after"] = sum(1 for r in g if r["after"] >= threshold)
    return out


def confusion(rows: List[Dict[str, Any]], key: str, threshold: float) -> Dict[str, Any]:
    """Unweighted decided-card confusion at `threshold` for score `key` ('before' | 'after'). PURE."""
    tp = sum(1 for r in rows if r["cls"] == "relevant" and r[key] >= threshold)
    fn = sum(1 for r in rows if r["cls"] == "relevant" and r[key] < threshold)
    fp = sum(1 for r in rows if r["cls"] == "irrelevant" and r[key] >= threshold)
    tn = sum(1 for r in rows if r["cls"] == "irrelevant" and r[key] < threshold)
    return {"relevant_above": tp, "relevant_below": fn, "irrelevant_above": fp, "irrelevant_below": tn,
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / (tp + fn), 3) if tp + fn else None}


def movements(rows: List[Dict[str, Any]], threshold: float) -> Dict[str, Any]:
    """How the cards moved across the threshold, by class. PURE."""
    out: Dict[str, Any] = {}
    for cls in ("relevant", "irrelevant", "excluded"):
        g = [r for r in rows if r["cls"] == cls]
        out[cls] = {
            "n": len(g),
            "below_before": sum(1 for r in g if r["before"] < threshold),
            "rose_above": sum(1 for r in g if r["before"] < threshold <= r["after"]),
            "above_before": sum(1 for r in g if r["before"] >= threshold),
            "fell_below": sum(1 for r in g if r["after"] < threshold <= r["before"]),
        }
    return out


def calibration(rows: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    out = {}
    for b in golden.BAND_NAMES:
        dec = [r for r in rows if golden.band_of(r[key]) == b and r["cls"] in ("relevant", "irrelevant")]
        k = sum(1 for r in dec if r["cls"] == "relevant")
        out[b] = {"decided": len(dec), "relevant": k}
    return out


def run_report(exclude_urls: Optional[str] = None) -> Dict[str, Any]:
    from evals.golden_eval import _frame_with_scores

    cfg, conn = db_ctx()
    T = cfg.surface_threshold
    devs = {r["item_id"]: r for r in dev_items(conn)}
    meta = {r["item_id"]: r for r in conn.execute(
        "SELECT DISTINCT ON (s.item_id) s.item_id, s.band, s.stratum, l.label, l.chips FROM eval_sample s "
        "JOIN eval_labels l ON l.sample_id = s.id WHERE NOT s.is_control ORDER BY s.item_id, s.wave").fetchall()}
    sc = collections.defaultdict(dict)
    for r in conn.execute("SELECT item_id, version, status, score FROM eval_prompt_scores").fetchall():
        if r["item_id"] in devs and r["status"] == "scored":
            sc[r["item_id"]][r["version"]] = r["score"]
    for r in conn.execute("SELECT item_id, status, current_score FROM eval_frame").fetchall():
        if r["item_id"] in devs and r["status"] == "scored":
            sc[r["item_id"]].setdefault("v1", r["current_score"])
    rows = []
    for i, v in sc.items():
        if "v1" in v and "v2" in v and meta[i]["stratum"] == "core":
            chips = [c for c in (meta[i]["chips"] or "").split(",") if c]
            rows.append({"item_id": i, "label": meta[i]["label"], "cls": golden.classify(meta[i]["label"], chips),
                         "before": v["v1"], "after": v["v2"], "stratum_band": meta[i]["band"]})
    out: Dict[str, Any] = {"dev_vacancies": len(devs), "paired_core": len(rows), "threshold": T,
                           "not_paired": len(devs) - len(rows),
                           "paired_all_labels": paired_metrics(rows, T)}
    out["confusion_unweighted"] = {"v1": confusion(rows, "before", T), "v2": confusion(rows, "after", T)}
    out["movements_across_threshold"] = movements(rows, T)
    out["calibration_by_score_band"] = {"v1": calibration(rows, "before"), "v2": calibration(rows, "after")}
    # design-weighted precision / recall: frame sizes scaled by the dev share of each band's cards
    _c, _sel, frame = _frame_with_scores(cfg, exclude_urls)
    sizes = collections.Counter(f["band"] for f in frame)
    allcore = collections.Counter(m["band"] for m in meta.values() if m["stratum"] == "core")
    devcore = collections.Counter(r["stratum_band"] for r in rows)
    scaled = {b: sizes.get(b, 0) * devcore.get(b, 0) / allcore[b] for b in allcore if allcore[b]}
    w = {}
    for ver, key in (("v1", "before"), ("v2", "after")):
        rr = [{**r, "score": r[key]} for r in rows]
        e = golden.design_estimate(rr, scaled, T)
        iv = golden.design_interval(rr, scaled, T, reps=1500)
        w[ver] = {"precision": None if e["precision"] is None else round(e["precision"], 3),
                  "precision_95": [round(x, 3) if x is not None else None for x in iv["precision"]],
                  "recall": None if e["recall"] is None else round(e["recall"], 3),
                  "recall_95": [round(x, 3) if x is not None else None for x in iv["recall"]]}
    out["design_weighted_at_T"] = {**w, "note": "frame sizes scaled to the dev share of each band: approximate; dev only"}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("diff")
    sub.add_parser("estimate")
    r = sub.add_parser("run")
    r.add_argument("--versions", default="v2")
    r.add_argument("--judge-share", type=float, default=0.30)
    add_budget_args(r)
    rp = sub.add_parser("report")
    rp.add_argument("--exclude-urls", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "diff":
        print(prompt_diff())
    elif a.cmd == "estimate":
        report("score prompt: cost of the dev iteration (no API)", run_estimate())
    elif a.cmd == "run":
        report("score prompt: paired dev run", run_score(a.versions.split(","), a.max_usd, a.confirm, a.judge_share))
    else:
        report("score prompt: paired v1 -> v2 (no API)", run_report(a.exclude_urls))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
