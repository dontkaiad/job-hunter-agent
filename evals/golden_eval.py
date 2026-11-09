"""evals/golden_eval.py -- the golden set: blind labels vs the CURRENT scorer.

  offline -- synthetic population: allocation, deterministic sample/waves/controls, the
             held-out split, label classes, the estimators (recover a known truth), and
             the blind-card whitelist. No API, no DB, no network (runs in CI).
  frame   -- live (SPENDS): score the sampling frame on the current system (Haiku first
             pass, Sonnet judge in the 50-70 corridor, temperature 0, forced tool), without
             touching work_items; results go to eval_frame, resumable, measured cost.
               python -m evals.golden_eval frame --exclude-urls FILE --max-usd N [--confirm]
             Prints the estimate and stops unless --confirm; hard stop at --max-usd.
  sample  -- NO API: build the two labelling waves from the frame using, per vacancy, the
             re-scored current score where eval_frame has one and the stored score
             otherwise (band_source records which). Needs only the database.
               python -m evals.golden_eval sample --exclude-urls FILE
  wave2   -- NO API: replace the (still unlabelled) wave 2 with the re-planned one
             (no hard stratum; 25-39 and 50-59 weighted; see golden.WAVE2_PLAN).
               python -m evals.golden_eval wave2 --exclude-urls FILE [--apply]   (dry-run without --apply)
  estimate-labeled -- NO API: what re-scoring the sampled cards would cost (skipping those
             already scored on the current system), sync vs Batch.
  rescore-labeled  -- live (SPENDS): score the sampled vacancies lacking a current-system
             score into eval_frame; --max-usd required, estimate first, --confirm to spend.
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

from evals.common import (
    BudgetExceeded, add_budget_args, db_ctx, est_score_call_usd, gate, make_client, print_usage_summary,
    report, save_result,
)
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
        "wave2_matches_the_plan": (
            lambda w2: len(w2) == 65 and sum(r["is_control"] for r in w2) == 5
            and not any(r["stratum"] == "hard" for r in w2)
            and collections.Counter(r["band"] for r in w2 if not r["is_control"]) == collections.Counter(golden.WAVE2_PLAN)
            and not ({r["item_id"] for r in w2} & {r["item_id"] for r in sample if r["wave"] == 1})
        )(golden.build_wave2(frame, {r["item_id"] for r in sample if r["wave"] == 1})),
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


def run_frame(exclude_urls: Optional[str], workers: int, limit: Optional[int], max_usd: float,
              confirm: bool, judge_share: float = 0.15) -> Dict[str, Any]:
    from evals.common import live_ctx
    from job_hunter import llm
    from job_hunter.clock import now_iso
    from job_hunter.schema_extract import from_dict
    from job_hunter import store

    cfg, profile, ro = live_ctx()
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
    # estimate from the REAL prompt sizes: every item once on Haiku, `judge_share` of them
    # again on Sonnet (measured: 64 of 456 = 14% in the first 27%)
    est = 0.0
    for f in todo:
        r = by_id[f["item_id"]]
        ex = from_dict(json.loads(r["extracted_json"]))
        est += est_score_call_usd(cfg.cheap_model, profile, ex, r["raw_text"], r.get("fetched_text"))
        est += judge_share * est_score_call_usd(cfg.judge_model, profile, ex, r["raw_text"], r.get("fetched_text"))
    print(f"frame {len(sel['frame'])} (excluded {sel['excluded']}); already scored {len(done)}; to score {len(todo)}",
          flush=True)
    gate("golden frame", est, max_usd, confirm, f"{len(todo)} vacancies, judge share assumed {judge_share:.0%}.")
    client = make_client(cfg, "golden_frame")

    def work(f):
        client.meter.thread_cost_reset()
        r = by_id[f["item_id"]]
        status, score, judge = "scored", None, False
        try:
            score, judge = score_current(client, from_dict(json.loads(r["extracted_json"])),
                                         r["raw_text"], r.get("fetched_text"), cfg, profile)
        except BudgetExceeded:
            return f, "stopped", None, False, client.meter.thread_cost()
        except llm.InsufficientContent:
            status = "insufficient"
        except Exception as exc:  # noqa: BLE001
            status = f"error:{type(exc).__name__}"
        return f, status, score, judge, client.meter.thread_cost()

    total_cost, n, stopped = 0.0, 0, False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(work, f) for f in todo]):
            f, status, score, judge, cost = fut.result()
            total_cost += cost
            if status == "stopped":
                stopped = True
                continue
            if status.startswith("error"):
                print(f"item {f['item_id']}: {status} (will be retried on the next run)", flush=True)
                continue
            rw.execute(
                "INSERT INTO eval_frame (item_id, stratum, status, current_score, band, judge_used, cost_usd, scored_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (item_id) DO NOTHING",
                (f["item_id"], f["stratum"], status, score, golden.band_of(score) if score is not None else None,
                 judge, cost, now_iso()))
            rw.commit()
            n += 1
            if n % 50 == 0:
                print(f"  scored {n}/{len(todo)}  measured ${client.meter.spent_usd():.2f} of ${max_usd:.2f}", flush=True)
    print_usage_summary(client.meter)
    out = {"frame_size": len(sel["frame"]), "excluded": sel["excluded"], "scored_this_run": n,
           "stopped_at_budget": stopped, "left_to_score": len(todo) - n,
           "cost": client.meter.report()}
    return out


def _frame_with_scores(cfg, exclude_urls: Optional[str]):
    """Frame rows {item_id, stratum, band, band_source}: the re-scored CURRENT score where
    eval_frame has one, the older STORED score otherwise. No API."""
    import psycopg

    conn = psycopg.connect(cfg.database_url, options="-c default_transaction_read_only=on",
                           row_factory=psycopg.rows.dict_row)
    restored = set(u.strip() for u in open(exclude_urls) if u.strip()) if exclude_urls else set()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, state, source_channel, source_link, raw_text, fetched_text, primary_url_key, created_at, "
        "relevance_score FROM work_items").fetchall()]
    hard = {r["item_id"] for r in conn.execute(
        "SELECT item_id FROM state_transitions WHERE to_state='rejected' AND reason LIKE 'hard reject%%'").fetchall()}
    cur = {r["item_id"]: r for r in conn.execute(
        "SELECT item_id, status, current_score FROM eval_frame").fetchall()}
    sel = select_frame(rows, hard, restored, cfg.link_only_min_chars)
    by_id = {r["id"]: r for r in rows}
    out = []
    for f in sel["frame"]:
        c = cur.get(f["item_id"])
        if c and c["status"] == "scored":
            band, src = golden.band_of(c["current_score"]), "current"
        else:
            band, src = golden.band_of(by_id[f["item_id"]]["relevance_score"]), "stored"
        out.append({**f, "band": band, "band_source": src})
    return conn, sel, out


def run_sample(exclude_urls: Optional[str]) -> Dict[str, Any]:
    """Build the waves WITHOUT any API call: the frame is banded by the re-scored current
    score where one exists (the first ~27%, mostly the OLDEST items) and by the stored
    score elsewhere. The band edges mean the same thing in both, but the two scores come
    from different system versions: that mix is a known bias, recorded per card."""
    from job_hunter import store
    from job_hunter.clock import now_iso

    cfg, _ro = db_ctx()
    conn, sel, frame = _frame_with_scores(cfg, exclude_urls)
    rw = store.connect(cfg.database_url)
    store.init_db(rw)
    rw.execute("ALTER TABLE eval_sample ADD COLUMN IF NOT EXISTS band_source TEXT")
    rw.commit()
    if rw.execute("SELECT count(*) AS n FROM eval_sample").fetchone()["n"]:
        raise SystemExit("eval_sample already exists; refusing to overwrite a golden set")
    sample = golden.build_sample(frame)
    ts = now_iso()
    for r in sample:
        rw.execute(
            "INSERT INTO eval_sample (item_id, wave, order_no, stratum, band, band_source, is_control, "
            "control_of_item, held_out, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (r["item_id"], r["wave"], r["order_no"], r["stratum"], r["band"], r.get("band_source"),
             r["is_control"], r["control_of_item"], r["held_out"], ts))
    rw.commit()
    core = [r for r in frame if r["stratum"] == "core"]
    plain = [r for r in sample if not r["is_control"] and r["stratum"] == "core"]
    return {
        "cards": len(sample), "by_wave": dict(collections.Counter(r["wave"] for r in sample)),
        "frame_excluded": sel["excluded"], "frame_core": len(core),
        "frame_band_sizes": dict(collections.Counter(r["band"] for r in core)),
        "frame_band_source": dict(collections.Counter(r["band_source"] for r in core)),
        "sample_core_by_band": dict(collections.Counter(r["band"] for r in plain)),
        "sample_core_by_band_source": dict(collections.Counter(f'{r["band"]}|{r["band_source"]}' for r in plain)),
        "held_out": sum(r["held_out"] for r in sample if not r["is_control"]),
        "note": "bands mix re-scored current scores (oldest ~27%) with stored scores from older system versions",
    }


def run_wave2(exclude_urls: Optional[str], apply: bool) -> Dict[str, Any]:
    """Rebuild wave 2 from the current frame, leaving wave 1 and any wave-2 card that is
    ALREADY labelled untouched (those stay, in place, and count toward the plan). Database
    only: no API."""
    from job_hunter import store
    from job_hunter.clock import now_iso

    cfg, _ro = db_ctx()
    conn, sel, frame = _frame_with_scores(cfg, exclude_urls)
    rw = store.connect(cfg.database_url)
    store.init_db(rw)
    taken = {r["item_id"] for r in rw.execute("SELECT item_id FROM eval_sample WHERE wave = 1").fetchall()}
    kept = [dict(r) for r in rw.execute(
        "SELECT s.id, s.order_no, s.item_id, s.band, s.band_source, s.stratum, s.is_control FROM eval_sample s "
        "JOIN eval_labels l ON l.sample_id = s.id WHERE s.wave = 2 ORDER BY s.order_no").fetchall()]
    if any(k["stratum"] != "core" or k["is_control"] for k in kept):
        raise SystemExit("refusing: a labelled wave-2 card is a control/hard card; handle it by hand")
    rows = golden.build_wave2(frame, taken, kept=kept)
    new_rows = [r for r in rows if r["item_id"] not in {k["item_id"] for k in kept} or r["is_control"]]
    plain = [r for r in rows if not r["is_control"]]
    out = {
        "applied": apply, "cards": len(rows), "controls": len(rows) - len(plain),
        "kept_already_labelled": len(kept), "new_cards": len(new_rows),
        "by_band": dict(collections.Counter(r["band"] for r in plain)),
        "by_band_source": dict(collections.Counter(f'{r["band"]}|{r["band_source"]}' for r in plain)),
        "held_out": sum(r["held_out"] for r in plain),
        "wave1_items_excluded": len(taken),
        "band_left_in_frame_after_wave1": dict(collections.Counter(
            r["band"] for r in frame if r["stratum"] == "core" and r["item_id"] not in taken)),
    }
    if apply:
        keep_ids = [k["id"] for k in kept]
        old = rw.execute("SELECT count(*) AS n FROM eval_sample WHERE wave = 2").fetchone()["n"]
        rw.execute("DELETE FROM eval_sample WHERE wave = 2 AND NOT (id = ANY(%s))", (keep_ids,))
        # the labelled cards keep their row (and label) but may move to their new position
        for k in kept:
            pos = next(r["order_no"] for r in rows if r["item_id"] == k["item_id"] and not r["is_control"])
            rw.execute("UPDATE eval_sample SET order_no = %s WHERE id = %s", (-pos, k["id"]))   # park clear of UNIQUE
        ts = now_iso()
        for r in new_rows:
            rw.execute(
                "INSERT INTO eval_sample (item_id, wave, order_no, stratum, band, band_source, is_control, "
                "control_of_item, held_out, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (r["item_id"], 2, r["order_no"], r["stratum"], r["band"], r.get("band_source"),
                 r["is_control"], r["control_of_item"], r["held_out"], ts))
        for k in kept:
            rw.execute("UPDATE eval_sample SET order_no = -order_no WHERE id = %s", (k["id"],))
        rw.commit()
        out["replaced_unlabelled_old_wave2_cards"] = old - len(kept)
    return out


def _labelled_todo(conn):
    """Unique sampled vacancies that still need a CURRENT-system score: those with no
    'scored' row in eval_frame (the first re-scored ~27% already have one)."""
    rows = conn.execute(
        "SELECT DISTINCT ON (s.item_id) s.item_id, s.stratum, w.raw_text, w.fetched_text, w.extracted_json "
        "FROM eval_sample s JOIN work_items w ON w.id = s.item_id "
        "LEFT JOIN eval_frame f ON f.item_id = s.item_id AND f.status = 'scored' "
        "WHERE f.item_id IS NULL ORDER BY s.item_id").fetchall()
    total = conn.execute("SELECT count(DISTINCT item_id) AS n FROM eval_sample").fetchone()["n"]
    return rows, int(total)


def run_estimate_labeled(judge_share: float = 0.15) -> Dict[str, Any]:
    """What re-scoring the sampled cards on the CURRENT system would cost. NO API.
    Vacancies already scored on the current system (eval_frame) are not counted."""
    from job_hunter.profile import load_profile
    from job_hunter.schema_extract import from_dict

    cfg, conn = db_ctx()
    profile = load_profile()
    rows, total = _labelled_todo(conn)
    out: Dict[str, Any] = {"sampled_vacancies": total, "already_scored_on_current_system": total - len(rows),
                           "to_score": len(rows), "judge_share_assumed": judge_share}
    for mode, batch in (("sync", False), ("batch_50pct", True)):
        usd = 0.0
        for r in rows:
            ex = from_dict(json.loads(r["extracted_json"]))
            usd += est_score_call_usd(cfg.cheap_model, profile, ex, r["raw_text"], r["fetched_text"], batch=batch)
            usd += judge_share * est_score_call_usd(cfg.judge_model, profile, ex, r["raw_text"],
                                                    r["fetched_text"], batch=batch)
        out[f"estimate_usd_{mode}"] = round(usd, 2)
    out["notes"] = ("Upper-bound style: no prompt-cache credit, offline token estimate x1.085 calibrated on "
                    "366 measured calls. Run it with: golden_eval rescore-labeled --max-usd N [--confirm] "
                    "(sync; the Batch runner is not built).")
    return out


def run_rescore_labeled(max_usd: float, confirm: bool, judge_share: float = 0.15) -> Dict[str, Any]:
    """Score the labelled vacancies that lack a current-system score (production route:
    Haiku, judge in the corridor), store them in eval_frame, never touching work_items."""
    from evals.common import live_ctx
    from job_hunter import llm, store
    from job_hunter.clock import now_iso
    from job_hunter.schema_extract import from_dict

    cfg, profile, ro = live_ctx()
    rows, total = _labelled_todo(ro)
    est = 0.0
    for r in rows:
        ex = from_dict(json.loads(r["extracted_json"]))
        est += est_score_call_usd(cfg.cheap_model, profile, ex, r["raw_text"], r["fetched_text"])
        est += judge_share * est_score_call_usd(cfg.judge_model, profile, ex, r["raw_text"], r["fetched_text"])
    gate("golden rescore-labeled", est, max_usd, confirm,
         f"{len(rows)} of {total} sampled vacancies need a current-system score.")
    client = make_client(cfg, "golden_rescore_labeled")
    rw = store.connect(cfg.database_url)
    store.init_db(rw)
    done, outcome = 0, collections.Counter()
    for r in rows:
        client.meter.thread_cost_reset()
        status, score, judge = "scored", None, False
        try:
            score, judge = score_current(client, from_dict(json.loads(r["extracted_json"])),
                                         r["raw_text"], r["fetched_text"], cfg, profile)
        except BudgetExceeded:
            outcome["stopped (budget)"] += 1
            break
        except llm.InsufficientContent:
            status = "insufficient"
        except Exception as exc:  # noqa: BLE001
            outcome[f"error {type(exc).__name__}"] += 1
            continue
        rw.execute(
            "INSERT INTO eval_frame (item_id, stratum, status, current_score, band, judge_used, cost_usd, scored_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (item_id) DO UPDATE SET status = EXCLUDED.status, "
            "current_score = EXCLUDED.current_score, band = EXCLUDED.band, judge_used = EXCLUDED.judge_used, "
            "cost_usd = EXCLUDED.cost_usd, scored_at = EXCLUDED.scored_at WHERE eval_frame.status <> 'scored'",
            (r["item_id"], r["stratum"], status, score, golden.band_of(score) if score is not None else None,
             judge, client.meter.thread_cost(), now_iso()))
        rw.commit()
        outcome[status] += 1
        done += 1
    print_usage_summary(client.meter)
    return {"to_score": len(rows), "done": done, "outcomes": dict(outcome), "cost": client.meter.report()}


def run_report(exclude_urls: Optional[str]) -> Dict[str, Any]:
    """Labels vs the CURRENT scorer (eval_frame). Read-only, no API. Aggregates only."""
    import collections
    cfg, _ro = db_ctx()
    conn, _sel, frame = _frame_with_scores(cfg, exclude_urls)
    sizes = collections.Counter(f["band"] for f in frame)
    cur = {r["item_id"]: r for r in conn.execute("SELECT item_id, status, current_score FROM eval_frame").fetchall()}
    stored = {r["id"]: r["relevance_score"] for r in conn.execute("SELECT id, relevance_score FROM work_items").fetchall()}
    cards = [dict(r) for r in conn.execute(
        "SELECT s.id, s.item_id, s.wave, s.stratum, s.band, s.is_control, s.control_of_item, l.label, l.chips "
        "FROM eval_sample s JOIN eval_labels l ON l.sample_id = s.id ORDER BY s.wave, s.order_no").fetchall()]
    rows, no_cur = [], 0
    for c in cards:
        if c["is_control"] or c["stratum"] != "core":
            continue
        f = cur.get(c["item_id"])
        if f and f["status"] == "scored":
            score = f["current_score"]
        elif f and f["status"] == "insufficient":
            score = golden.NO_SCORE
        else:
            score, no_cur = (stored.get(c["item_id"]) or 0.0), no_cur + 1
        chips = [x for x in (c["chips"] or "").split(",") if x]
        rows.append({"item_id": c["item_id"], "stratum_band": c["band"], "score": float(score),
                     "label": c["label"], "chips": chips, "cls": golden.classify(c["label"], chips)})
    out: Dict[str, Any] = {
        "note": "design-weighted by the sampling band; frame sizes use the CURRENT-score band (approximation: "
                "a few vacancies changed band after the re-score)",
        "labelled_core_vacancies": len(rows), "scored_without_current_score": no_cur,
        "frame_band_sizes": dict(sizes),
        "classes": dict(collections.Counter(r["cls"] for r in rows)),
    }
    rnd = lambda v: None if v is None else round(v, 3)
    for name, sens in (("main", False), ("sensitivity_geo_stale_pay_other_count_as_irrelevant", True)):
        res = {}
        for t in (50, 55, 60, 65, 70):
            e = golden.design_estimate(rows, dict(sizes), t, sensitivity=sens)
            iv = golden.design_interval(rows, dict(sizes), t, sensitivity=sens, reps=1500)
            res[f"T{t}"] = {"precision": rnd(e["precision"]), "precision_95": [rnd(x) for x in iv["precision"]],
                            "recall": rnd(e["recall"]), "recall_95": [rnd(x) for x in iv["recall"]]}
        out[name] = res
    out["label_noise_precision_recall_at_60"] = {
        f"flip_{int(eps*100)}pct": {k: rnd(v) for k, v in golden.design_estimate(rows, dict(sizes), 60, noise=eps).items()
                                    if k in ("precision", "recall")} for eps in (0.0, 0.05, 0.10, 0.15)}
    # calibration by CURRENT score band (unweighted share among decided; label counts, not a population claim)
    cal = collections.OrderedDict()
    for b in golden.BAND_NAMES:
        grp = [r for r in rows if golden.band_of(r["score"]) == b]
        dec = [r for r in grp if r["cls"] in ("relevant", "irrelevant")]
        k = sum(1 for r in dec if r["cls"] == "relevant")
        lo, hi = golden.wilson(k, len(dec))
        cal[b] = {"cards": len(grp), "decided": len(dec), "relevant": k,
                  "share": rnd(k / len(dec)) if dec else None, "wilson_95": [rnd(lo), rnd(hi)]}
    out["calibration_by_current_score"] = cal
    # reasons: what the cards at/above 60 and in 50-59 were rejected for
    def reasons(lo_, hi_):
        grp = [r for r in rows if lo_ <= r["score"] < hi_]
        c = collections.Counter()
        for r in grp:
            c["yes" if r["label"] == "yes" else "unsure" if r["label"] == "unsure"
              else ("+".join(sorted(r["chips"])) or "no chip")] += 1
        return {"cards": len(grp), "by_label_or_chips": dict(c.most_common())}
    out["reasons_score_60_plus"] = reasons(60, 1000)
    out["reasons_score_50_59"] = reasons(50, 60)
    # control pairs -> label noise
    first = {}
    for c in cards:
        if not c["is_control"]:
            first.setdefault(c["item_id"], c)
    a3, b3, a2, b2 = [], [], [], []
    for c in cards:
        if c["is_control"] and c["control_of_item"] in first:
            o = first[c["control_of_item"]]
            a3.append(o["label"]); b3.append(c["label"])
            if "unsure" not in (o["label"], c["label"]):
                a2.append(o["label"]); b2.append(c["label"])
    po3, k3 = golden.cohen_kappa(a3, b3, ("yes", "no", "unsure"))
    po2, k2 = golden.cohen_kappa(a2, b2, ("yes", "no"))
    out["label_noise_control_pairs"] = {"pairs": len(a3), "agreement_3class": rnd(po3), "kappa_3class": rnd(k3),
                                        "pairs_binary": len(a2), "agreement_binary": rnd(po2), "kappa_binary": rnd(k2)}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("offline")
    f = sub.add_parser("frame")
    f.add_argument("--exclude-urls", default=None)
    f.add_argument("--workers", type=int, default=4)
    f.add_argument("--limit", type=int, default=None)
    f.add_argument("--judge-share", type=float, default=0.15)
    add_budget_args(f)
    sm = sub.add_parser("sample")
    sm.add_argument("--exclude-urls", default=None)
    w2 = sub.add_parser("wave2")
    w2.add_argument("--exclude-urls", default=None)
    w2.add_argument("--apply", action="store_true")
    sub.add_parser("estimate-labeled")
    rp = sub.add_parser("report")
    rp.add_argument("--exclude-urls", default=None)
    rl = sub.add_parser("rescore-labeled")
    rl.add_argument("--judge-share", type=float, default=0.15)
    add_budget_args(rl)
    a = ap.parse_args(argv)
    if a.cmd == "offline":
        report("golden set (offline)", run_offline())
    elif a.cmd == "frame":
        report("golden set: frame", run_frame(a.exclude_urls, a.workers, a.limit, a.max_usd, a.confirm,
                                              a.judge_share))
    elif a.cmd == "sample":
        report("golden set: sample (no API)", run_sample(a.exclude_urls))
    elif a.cmd == "wave2":
        report("golden set: wave 2 re-plan (no API)", run_wave2(a.exclude_urls, a.apply))
    elif a.cmd == "rescore-labeled":
        report("golden set: re-score the labelled vacancies", run_rescore_labeled(a.max_usd, a.confirm, a.judge_share))
    elif a.cmd == "report":
        report("golden set: labels vs the current scorer (no API)", run_report(a.exclude_urls))
    elif a.cmd == "estimate-labeled":
        report("golden set: cost of re-scoring the labelled cards (no API)", run_estimate_labeled())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
