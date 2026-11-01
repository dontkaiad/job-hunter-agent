"""evals/golden_eval.py -- the golden set: blind labels vs the CURRENT scorer.

  offline -- synthetic population: allocation, deterministic sample/waves/controls, the
             held-out split, label classes, the estimators (recover a known truth), and
             the blind-card whitelist. No API, no DB, no network (runs in CI).
  frame   -- live: score the sampling frame on the current system (Haiku first pass,
             Sonnet judge in the 50-70 corridor, temperature 0, forced tool), without
             touching work_items; results go to eval_frame, resumable, measured cost per
             item from API usage.
               python -m evals.golden_eval frame --exclude-urls FILE [--workers 4] [--limit N]
  sample  -- live: build the two labelling waves from eval_frame into eval_sample.
  report  -- live: metrics from eval_labels (precision@60, recall, calibration by band,
             PR curve, by reason and source, held-out kept separate). Aggregates only.

The operator's labels and the vacancy texts never leave the database.
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from evals.common import report, save_result
from job_hunter import golden

RESTORED_FROM = "2026-10-29"   # add_by_url re-ingests of the 2026-10 outage start here
NOT_YET_SCORED = ("discovered", "extracted", "scored")


def select_frame(rows: Sequence[Dict[str, Any]], hard_ids: Set[int], restored_urls: Set[str],
                 link_only_min_chars: int = 40) -> Dict[str, Any]:
    """Which vacancies enter the sampling frame, and why the rest do not. PURE.

    rows: work_items {id, state, source_channel, source_link, raw_text, fetched_text,
    primary_url_key, created_at}. Hard rejects form their own stratum; duplicates keep
    the OLDEST id; restored-outage rows, unscored rows, empty text and link-only stubs
    with no fetched text are out."""
    from job_hunter import scoring

    excl: collections.Counter = collections.Counter()
    seen: Set[str] = set()
    frame: List[Dict[str, Any]] = []
    for r in sorted(rows, key=lambda r: r["id"]):
        if r["state"] in NOT_YET_SCORED:
            excl["not scored yet"] += 1
        elif (r["source_channel"] == "manual" and (r["created_at"] or "") >= RESTORED_FROM
              and r["source_link"] in restored_urls):
            excl["restored after the outage"] += 1
        elif not (r["raw_text"] or "").strip():
            excl["no text"] += 1
        elif r["primary_url_key"] and r["primary_url_key"] in seen:
            excl["duplicate of an older row"] += 1
        elif scoring.is_link_only(r["raw_text"], link_only_min_chars) and not r.get("fetched_text"):
            excl["link-only stub, no page text"] += 1
        else:
            if r["primary_url_key"]:
                seen.add(r["primary_url_key"])
            frame.append({"item_id": r["id"], "stratum": "hard" if r["id"] in hard_ids else "core"})
    return {"frame": frame, "excluded": dict(excl)}


# --- offline ----------------------------------------------------------------

def _synthetic_population(rnd: random.Random, true_share: Dict[str, float]):
    sizes = {"<25": 700, "25-39": 500, "40-49": 150, "50-59": 80, "60-69": 60, "70-79": 70, "80+": 20}
    frame, truth, i = [], {}, 1
    for b, n in sizes.items():
        for _ in range(n):
            frame.append({"item_id": i, "band": b, "stratum": "core", "score": {"<25": 10, "25-39": 30, "40-49": 45,
                          "50-59": 55, "60-69": 65, "70-79": 75, "80+": 85}[b]})
            truth[i] = rnd.random() < true_share[b]
            i += 1
    for _ in range(136):
        frame.append({"item_id": i, "band": "<25", "stratum": "hard", "score": 20})
        truth[i] = rnd.random() < 0.05
        i += 1
    return sizes, frame, truth


def run_offline() -> Dict[str, Any]:
    rnd = random.Random(42)
    share = {"<25": .01, "25-39": .03, "40-49": .08, "50-59": .20, "60-69": .35, "70-79": .55, "80+": .75}
    sizes, frame, truth = _synthetic_population(rnd, share)
    sample = golden.build_sample(frame)
    per_wave = collections.Counter(r["wave"] for r in sample)
    core_orig = [r for r in sample if r["stratum"] == "core" and not r["is_control"]]
    controls = [r for r in sample if r["is_control"]]
    # every control comes >= GAP cards after its original, in the same wave, from the dev split
    gap_ok = True
    for c in controls:
        first = next(r for r in sample if r["wave"] == c["wave"] and r["item_id"] == c["control_of_item"]
                     and not r["is_control"])
        gap_ok &= (c["order_no"] - first["order_no"] >= golden.CONTROL_MIN_GAP)
    ho = sum(r["held_out"] for r in core_orig) / len(core_orig)
    by_item = {r["item_id"]: r for r in frame}
    # estimators: over several synthetic labelling runs the 95% intervals must cover the
    # (known) truth most of the time, and the point estimates must not drift
    hits, errs = 0, []
    runs = 16
    for seed in range(runs):
        rr = random.Random(1000 + seed)
        sz, fr, tr = _synthetic_population(rr, share)
        smp = [r for r in golden.build_sample(fr) if r["stratum"] == "core" and not r["is_control"]]
        sc = {r["item_id"]: r for r in fr}
        lab = [{"band": r["band"], "score": sc[r["item_id"]]["score"],
                "cls": "relevant" if tr[r["item_id"]] else "irrelevant"} for r in smp]
        est = golden.estimate(lab, sz)
        ci = golden.interval(lab, sz, reps=300, seed=seed)
        tp = sum(tr[r["item_id"]] for r in fr if r["stratum"] == "core" and r["band"] in golden.SURFACE_BANDS)
        al = sum(tr[r["item_id"]] for r in fr if r["stratum"] == "core")
        t_rec, t_prec = tp / al, tp / sum(sz[b] for b in golden.SURFACE_BANDS)
        hits += (ci["recall_at_60"][0] <= t_rec <= ci["recall_at_60"][1]
                 and ci["precision_at_60"][0] <= t_prec <= ci["precision_at_60"][1])
        errs.append(est["precision_at_60"] - t_prec)
    labels = [{"band": r["band"], "score": by_item[r["item_id"]]["score"],
               "cls": "relevant" if truth[r["item_id"]] else "irrelevant"} for r in core_orig]
    card = golden.card_payload(sample_id=1, position=1, total=70, wave=1,
                               extracted={"title": "t", "company": "c", "score": 99, "Обоснование": "secret"},
                               text="x" * 700, link=None)
    classes = {
        "yes": golden.classify("yes", []),
        "no_plain": golden.classify("no", []),
        "no_fit": golden.classify("no", ["fit", "stale"]),
        "no_stale_pay": golden.classify("no", ["stale", "pay"]),
        "no_geo": golden.classify("no", ["geo"]),
        "no_other": golden.classify("no", ["other"]),
        "unsure": golden.classify("unsure", []),
    }
    pr = golden.pr_curve(labels, sizes)
    sel = select_frame(
        [{"id": 1, "state": "rejected", "source_channel": "@a", "source_link": "x", "raw_text": "AI engineer wanted, remote work, python",
          "fetched_text": None, "primary_url_key": "k", "created_at": "2026-09-01"},
         {"id": 2, "state": "rejected", "source_channel": "@b", "source_link": "y", "raw_text": "AI engineer wanted, remote work, python",
          "fetched_text": None, "primary_url_key": "k", "created_at": "2026-09-02"},
         {"id": 3, "state": "extracted", "source_channel": "@a", "source_link": "z", "raw_text": "t", "fetched_text": None,
          "primary_url_key": None, "created_at": "2026-09-02"},
         {"id": 4, "state": "rejected", "source_channel": "manual", "source_link": "https://r", "raw_text": "AI engineer wanted, remote work, python",
          "fetched_text": None, "primary_url_key": None, "created_at": "2026-10-30"}],
        hard_ids=set(), restored_urls={"https://r"})
    return {
        "sample_size_is_145": len(sample) == 145,
        "waves_are_70_and_75": (per_wave[1], per_wave[2]) == (70, 75),
        "core_120_hard_10_controls_15": (len(core_orig), sum(r["stratum"] == "hard" for r in sample if not r["is_control"]),
                                         len(controls)) == (120, 10, 15),
        "controls_follow_originals_by_gap": gap_ok,
        "controls_only_from_dev_split": all(not r["held_out"] for r in controls),
        "no_duplicate_cards_except_controls": len({r["item_id"] for r in sample if not r["is_control"]}) ==
                                              len([r for r in sample if not r["is_control"]]),
        "sample_is_deterministic": sample == golden.build_sample(list(reversed(frame))),
        "held_out_share_about_30pct": 0.2 < ho < 0.4,
        "intervals_cover_truth_in_most_runs": hits / runs >= 0.75,
        "interval_coverage_joint_precision_and_recall": hits / runs,
        "precision_estimate_is_not_biased": abs(sum(errs) / len(errs)) < 0.05,
        "label_classes": classes,
        "label_classes_ok": classes == {"yes": "relevant", "no_plain": "irrelevant", "no_fit": "irrelevant",
                                        "no_stale_pay": "excluded", "no_geo": "excluded", "no_other": "other",
                                        "unsure": "unsure"},
        "pr_curve_has_all_thresholds": len(pr) == 6,
        "blind_card_has_no_score_or_source": set(card) == set(golden.CARD_FIELDS)
                                             and "score" not in card and "Обоснование" not in json.dumps(card, ensure_ascii=False),
        "frame_rules_ok": ([f["item_id"] for f in sel["frame"]] == [1] and sel["excluded"] == {
            "duplicate of an older row": 1, "not scored yet": 1, "restored after the outage": 1}),
    }


# --- live -------------------------------------------------------------------

def score_current(client, ex, raw: str, fetched: Optional[str], cfg, profile):
    """The production scoring route: Haiku first pass; in the corridor the Sonnet judge
    decides (and on a judge failure the Haiku score stands). Raises InsufficientContent."""
    from job_hunter import llm

    h = llm.llm_score(client, ex, raw, model=cfg.cheap_model, profile=profile, fetched_text=fetched)["score"]
    if cfg.score_corridor_lo <= h <= cfg.score_corridor_hi:
        try:
            return llm.llm_score(client, ex, raw, model=cfg.judge_model, profile=profile,
                                 fetched_text=fetched)["score"], True
        except Exception:  # noqa: BLE001
            return h, False
    return h, False


def run_frame(exclude_urls: Optional[str], workers: int, limit: Optional[int]) -> Dict[str, Any]:
    from evals.common import UsageMeter, live_env, usage_cost
    from job_hunter import llm
    from job_hunter.clock import now_iso
    from job_hunter.schema_extract import from_dict
    import psycopg
    from job_hunter import store

    cfg, profile, _client, ro = live_env()
    restored = set(u.strip() for u in open(exclude_urls) if u.strip()) if exclude_urls else set()
    rows = [dict(r) for r in ro.execute(
        "SELECT id, state, source_channel, source_link, raw_text, fetched_text, primary_url_key, created_at, "
        "extracted_json FROM work_items").fetchall()]
    hard = {r["item_id"] for r in ro.execute(
        "SELECT item_id FROM state_transitions WHERE to_state='rejected' AND reason LIKE 'hard reject%%'").fetchall()}
    sel = select_frame(rows, hard, restored, cfg.link_only_min_chars)
    by_id = {r["id"]: r for r in rows}
    rw = store.connect(cfg.database_url)
    store.init_db(rw)  # creates the eval tables if missing
    done = {r["item_id"] for r in rw.execute("SELECT item_id FROM eval_frame").fetchall()}
    todo = [f for f in sel["frame"] if f["item_id"] not in done]
    if limit:
        todo = todo[:limit]
    print(f"frame {len(sel['frame'])} (excluded {sel['excluded']}); already scored {len(done)}; to score {len(todo)}",
          flush=True)

    local = threading.local()

    def work(f):
        if not hasattr(local, "client"):          # one client + meter per worker thread
            local.client = llm.AnthropicClient(cfg.anthropic_api_key, cfg.cheap_model)
            local.meter = UsageMeter().attach(local.client)
        r = by_id[f["item_id"]]
        before = len(local.meter.calls)
        status, score, judge = "scored", None, False
        try:
            score, judge = score_current(local.client, from_dict(json.loads(r["extracted_json"])),
                                         r["raw_text"], r.get("fetched_text"), cfg, profile)
        except llm.InsufficientContent:
            status = "insufficient"
        except Exception as exc:  # noqa: BLE001
            status = f"error:{type(exc).__name__}"
        cost = sum(usage_cost(m, u) or 0 for m, u in local.meter.calls[before:])
        return f, status, score, judge, cost

    total_cost, n = 0.0, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(work, f) for f in todo]):
            f, status, score, judge, cost = fut.result()
            if status.startswith("error"):
                print(f"item {f['item_id']}: {status} (will be retried on the next run)", flush=True)
                total_cost += cost
                continue
            rw.execute(
                "INSERT INTO eval_frame (item_id, stratum, status, current_score, band, judge_used, cost_usd, scored_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (item_id) DO NOTHING",
                (f["item_id"], f["stratum"], status, score, golden.band_of(score) if score is not None else None,
                 judge, cost, now_iso()))
            rw.commit()
            total_cost += cost
            n += 1
            if n % 50 == 0:
                print(f"  scored {n}/{len(todo)}  measured cost so far ${total_cost:.2f}", flush=True)
    summary = rw.execute(
        "SELECT status, stratum, band, count(*) AS n, sum(cost_usd) AS usd FROM eval_frame GROUP BY 1,2,3 ORDER BY 2,3").fetchall()
    out = {"frame_size": len(sel["frame"]), "excluded": sel["excluded"], "scored_this_run": n,
           "measured_cost_usd_this_run": round(total_cost, 4),
           "measured_cost_usd_total": round(float(rw.execute("SELECT coalesce(sum(cost_usd),0) AS s FROM eval_frame").fetchone()["s"]), 4),
           "by_status_stratum_band": [dict(r) for r in summary]}
    for r in out["by_status_stratum_band"]:
        r["usd"] = round(float(r["usd"] or 0), 4)
    return out


def run_sample() -> Dict[str, Any]:
    from evals.common import live_env
    from job_hunter import store
    from job_hunter.clock import now_iso

    cfg, _p, _c, _ro = live_env()
    rw = store.connect(cfg.database_url)
    store.init_db(rw)
    if rw.execute("SELECT count(*) AS n FROM eval_sample").fetchone()["n"]:
        raise SystemExit("eval_sample already exists; refusing to overwrite a golden set")
    fr = rw.execute("SELECT item_id, stratum, band FROM eval_frame WHERE status = 'scored'").fetchall()
    sample = golden.build_sample([dict(r) for r in fr])
    ts = now_iso()
    for r in sample:
        rw.execute(
            "INSERT INTO eval_sample (item_id, wave, order_no, stratum, band, is_control, control_of_item, held_out, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (r["item_id"], r["wave"], r["order_no"], r["stratum"], r["band"], r["is_control"],
             r["control_of_item"], r["held_out"], ts))
    rw.commit()
    return {"cards": len(sample), "by_wave": dict(collections.Counter(r["wave"] for r in sample)),
            "by_band_core": dict(collections.Counter(r["band"] for r in sample if r["stratum"] == "core" and not r["is_control"])),
            "frame_band_sizes": dict(collections.Counter(r["band"] for r in fr if r["stratum"] == "core")),
            "held_out": sum(r["held_out"] for r in sample if not r["is_control"])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("offline")
    f = sub.add_parser("frame")
    f.add_argument("--exclude-urls", default=None)
    f.add_argument("--workers", type=int, default=4)
    f.add_argument("--limit", type=int, default=None)
    sub.add_parser("sample")
    a = ap.parse_args(argv)
    if a.cmd == "offline":
        report("golden set (offline)", run_offline())
    elif a.cmd == "frame":
        report("golden set: frame", run_frame(a.exclude_urls, a.workers, a.limit))
    elif a.cmd == "sample":
        report("golden set: sample", run_sample())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
