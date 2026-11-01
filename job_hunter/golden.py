"""PURE: the golden set for the scoring eval -- strata, deterministic sampling, labels,
estimators, and the blind card payload.

The operator labels a stratified sample of vacancies BLIND (no score, no rationale, no
source) in the dashboard. Labels live in the DB (eval_labels) and never in the repo.
Everything here is deterministic (sha1 of the id + a salt): the same frame always gives
the same sample, waves and held-out split.
"""
from __future__ import annotations

import hashlib
import math
import random
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SALT = "golden-v1"

# (name, lo, hi) over the CURRENT score; >=60 is the surface threshold.
BANDS: List[Tuple[str, float, float]] = [
    ("<25", float("-inf"), 25), ("25-39", 25, 40), ("40-49", 40, 50), ("50-59", 50, 60),
    ("60-69", 60, 70), ("70-79", 70, 80), ("80+", 80, float("inf")),
]
BAND_NAMES = [b[0] for b in BANDS]
SURFACE_BANDS = ("60-69", "70-79", "80+")
CORE_TARGET: Dict[str, int] = {"<25": 12, "25-39": 20, "40-49": 20, "50-59": 16,
                               "60-69": 20, "70-79": 20, "80+": 12}      # 120
HARD_TARGET = 10
# wave 1 = 70 cards (55 core + 5 hard + 10 controls); wave 2 = 75 (65 + 5 + 5)
WAVE1_CORE, WAVE1_HARD, WAVE1_CONTROLS = 55, 5, 10
WAVE2_CONTROLS = 5
CONTROL_MIN_GAP = 10          # a control repeats an item shown at least this many cards earlier
HELD_OUT_PERCENT = 30


def band_of(score: Optional[float]) -> str:
    s = -1.0 if score is None else float(score)
    for name, lo, hi in BANDS:
        if lo <= s < hi:
            return name
    return BANDS[-1][0]


def _h(item_id: int, tag: str) -> int:
    return int(hashlib.sha1(f"{item_id}:{SALT}:{tag}".encode()).hexdigest(), 16)


def rank_key(item_id: int) -> int:
    """Stable pseudo-random order of ids (the sampling order)."""
    return _h(item_id, "rank")


def held_out(item_id: int) -> bool:
    """30% of ids are held out: never used to tune prompts, looked at once per release."""
    return _h(item_id, "holdout") % 100 < HELD_OUT_PERCENT


# --- allocation -------------------------------------------------------------

def allocate(band_sizes: Dict[str, int], target: Dict[str, int] = CORE_TARGET) -> Dict[str, int]:
    """n per band: the target, capped at the band size (census), the shortfall handed to
    bands with spare capacity in proportion to their target. Deterministic."""
    n = {b: min(target[b], band_sizes.get(b, 0)) for b in BAND_NAMES}
    short = sum(target.values()) - sum(n.values())
    while short > 0:
        room = {b: band_sizes.get(b, 0) - n[b] for b in BAND_NAMES if band_sizes.get(b, 0) > n[b]}
        if not room:
            break
        tot = sum(target[b] for b in room)
        gave = 0
        for b in sorted(room, key=lambda x: (-target[x], x)):
            add = min(room[b], max(1, round(short * target[b] / tot)))
            add = min(add, short - gave)
            n[b] += add
            gave += add
            if gave >= short:
                break
        if gave == 0:
            break
        short -= gave
    return n


# --- sample -----------------------------------------------------------------

def build_sample(frame: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Frame rows {item_id, band, stratum('core'|'hard')} -> ordered sample rows
    {wave, order_no, item_id, stratum, band, is_control, control_of_item, held_out}.

    Core: per band, the first n_b ids in hash order; the first ~46% of each band go to
    wave 1 (so wave 1 alone is a smaller stratified sample). Hard rejects: first 10 in hash
    order, 5 per wave. Controls are repeats of DEV (non-held-out) core items, inserted at
    least CONTROL_MIN_GAP cards after the original.
    """
    core = [r for r in frame if r["stratum"] == "core"]
    sizes = {b: sum(1 for r in core if r["band"] == b) for b in BAND_NAMES}
    n_b = allocate(sizes)
    # wave-1 quota per band: proportional, largest remainder, summing to WAVE1_CORE
    # (or to everything when the sample is smaller than the plan)
    want1 = min(WAVE1_CORE, round(sum(n_b.values()) * WAVE1_CORE / sum(CORE_TARGET.values())))
    exact = {b: n_b[b] * want1 / max(1, sum(n_b.values())) for b in BAND_NAMES}
    k1 = {b: int(exact[b]) for b in BAND_NAMES}
    for b in sorted(BAND_NAMES, key=lambda x: (-(exact[x] - k1[x]), x))[: want1 - sum(k1.values())]:
        k1[b] += 1
    picked: List[Dict[str, Any]] = []
    for b in BAND_NAMES:
        rows = sorted((r for r in core if r["band"] == b), key=lambda r: rank_key(r["item_id"]))[: n_b[b]]
        for j, r in enumerate(rows):
            picked.append({**r, "wave": 1 if j < k1[b] else 2})
    hard = sorted((r for r in frame if r["stratum"] == "hard"), key=lambda r: rank_key(r["item_id"]))[:HARD_TARGET]
    for j, r in enumerate(hard):
        picked.append({**r, "wave": 1 if j < WAVE1_HARD else 2})
    out: List[Dict[str, Any]] = []
    for wave, n_controls in ((1, WAVE1_CONTROLS), (2, WAVE2_CONTROLS)):
        cards = sorted((r for r in picked if r["wave"] == wave), key=lambda r: rank_key(r["item_id"]))
        seq = [{"item_id": r["item_id"], "stratum": r["stratum"], "band": r["band"],
                "is_control": False, "control_of_item": None} for r in cards]
        dev = [i for i, r in enumerate(seq) if r["stratum"] == "core" and not held_out(r["item_id"])
               and i <= len(seq) - 1 - CONTROL_MIN_GAP]
        dev.sort(key=lambda i: rank_key(seq[i]["item_id"]) ^ 0x5A5A)
        for i in dev[:n_controls]:
            seq.append({**seq[i], "is_control": True, "control_of_item": seq[i]["item_id"], "_after": i})
        # place each control >= GAP cards after its original, spread over the tail
        base = [r for r in seq if not r["is_control"]]
        ctrl = sorted((r for r in seq if r["is_control"]), key=lambda r: r["_after"])
        final = list(base)
        for c in ctrl:
            orig_pos = next(i for i, r in enumerate(final) if r["item_id"] == c["control_of_item"] and not r["is_control"])
            lo = min(orig_pos + CONTROL_MIN_GAP, len(final))
            pos = lo + (rank_key(c["item_id"]) % (len(final) - lo + 1))
            final.insert(pos, {k: v for k, v in c.items() if k != "_after"})
        for no, r in enumerate(final, 1):
            out.append({"wave": wave, "order_no": no, "held_out": held_out(r["item_id"]), **r})
    return out


# --- labels -----------------------------------------------------------------

LABELS = ("yes", "no", "unsure")
CHIP_CODES = ("stale", "geo", "fit", "seniority", "pay", "other")


def classify(label: str, chips: Iterable[str]) -> str:
    """relevant | irrelevant | excluded | other | unsure  (the main-metric class).

    yes -> relevant. no -> irrelevant when it has fit/seniority or no chip at all;
    excluded when it carries only stale/geo/pay (not a verdict on role fit: the scorer
    cannot know freshness, salary is a deterministic guard, geo/visa is a guard signal);
    other when «другое» is the only substantive reason. unsure stays separate."""
    if label == "yes":
        return "relevant"
    if label == "unsure":
        return "unsure"
    c = set(chips or ())
    if c & {"fit", "seniority"} or not c:
        return "irrelevant"
    if "other" in c:
        return "other"
    return "excluded"


# --- estimators -------------------------------------------------------------

def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def _rates(labels: Sequence[Dict[str, Any]], sensitivity: bool) -> Dict[str, Tuple[int, int]]:
    """band -> (relevant, decided) from labelled core items."""
    out: Dict[str, List[int]] = {b: [0, 0] for b in BAND_NAMES}
    for r in labels:
        cls = r["cls"]
        if sensitivity and cls in ("excluded", "other", "unsure"):
            cls = "irrelevant"
        if cls not in ("relevant", "irrelevant"):
            continue
        out[r["band"]][1] += 1
        out[r["band"]][0] += cls == "relevant"
    return {b: (v[0], v[1]) for b, v in out.items()}


def estimate(labels: Sequence[Dict[str, Any]], band_sizes: Dict[str, int], *,
             sensitivity: bool = False) -> Dict[str, Any]:
    """Population precision@60, recall, per-band relevant share from the stratified sample.

    labels: core-stratum rows {band, cls}. band_sizes: the frame's N per band (current
    score). A band with no decided labels contributes 0 relevant (reported in `empty_bands`)."""
    rt = _rates(labels, sensitivity)
    share = {b: (rt[b][0] / rt[b][1] if rt[b][1] else 0.0) for b in BAND_NAMES}
    tp = sum(band_sizes.get(b, 0) * share[b] for b in SURFACE_BANDS)
    fn = sum(band_sizes.get(b, 0) * share[b] for b in BAND_NAMES if b not in SURFACE_BANDS)
    n_surf = sum(band_sizes.get(b, 0) for b in SURFACE_BANDS)
    return {
        "precision_at_60": tp / n_surf if n_surf else None,
        "recall_at_60": tp / (tp + fn) if (tp + fn) > 0 else None,
        "est_relevant_surfaced": tp, "est_relevant_missed": fn,
        "per_band": {b: {"relevant": rt[b][0], "decided": rt[b][1], "share": share[b],
                         "wilson95": wilson(rt[b][0], rt[b][1]), "frame_n": band_sizes.get(b, 0)}
                     for b in BAND_NAMES},
        "empty_bands": [b for b in BAND_NAMES if rt[b][1] == 0 and band_sizes.get(b, 0) > 0],
    }


def interval(labels: Sequence[Dict[str, Any]], band_sizes: Dict[str, int], *,
             sensitivity: bool = False, reps: int = 2000, seed: int = 20261101) -> Dict[str, Tuple[float, float]]:
    """95% intervals for precision@60 and recall@60.

    Each band's relevant share gets a Jeffreys Beta(k+.5, n-k+.5) posterior, so a band
    with 0 relevant out of n keeps honest uncertainty (a plain bootstrap would give it
    none); the surfaced/missed counts are then recomputed from the draws. Percentile
    interval over `reps` draws."""
    rnd = random.Random(seed)
    rt = _rates(labels, sensitivity)
    prec, rec = [], []
    n_surf = sum(band_sizes.get(b, 0) for b in SURFACE_BANDS)
    for _ in range(reps):
        p = {b: (rnd.betavariate(rt[b][0] + 0.5, rt[b][1] - rt[b][0] + 0.5) if rt[b][1] else 0.0)
             for b in BAND_NAMES}
        tp = sum(band_sizes.get(b, 0) * p[b] for b in SURFACE_BANDS)
        fn = sum(band_sizes.get(b, 0) * p[b] for b in BAND_NAMES if b not in SURFACE_BANDS)
        if n_surf:
            prec.append(tp / n_surf)
        if tp + fn > 0:
            rec.append(tp / (tp + fn))

    def ci(v):
        v = sorted(v)
        return (v[int(0.025 * len(v))], v[min(len(v) - 1, int(0.975 * len(v)))]) if v else (None, None)

    return {"precision_at_60": ci(prec), "recall_at_60": ci(rec)}


def pr_curve(labels: Sequence[Dict[str, Any]], band_sizes: Dict[str, int],
             thresholds: Sequence[float] = (25, 40, 50, 60, 70, 80), *, sensitivity: bool = False) -> List[Dict[str, Any]]:
    """Weighted precision/recall by score threshold. Each labelled item stands for
    N_band / n_band frame items. labels need {band, cls, score}."""
    n_b = {b: sum(1 for r in labels if r["band"] == b) for b in BAND_NAMES}
    rows = []
    for r in labels:
        cls = "irrelevant" if sensitivity and r["cls"] in ("excluded", "other", "unsure") else r["cls"]
        if cls in ("relevant", "irrelevant"):
            rows.append((r["score"], band_sizes.get(r["band"], 0) / max(1, n_b[r["band"]]), cls == "relevant"))
    total_rel = sum(w for _, w, rel in rows if rel)
    out = []
    for t in thresholds:
        sel = [(w, rel) for s, w, rel in rows if s >= t]
        tp = sum(w for w, rel in sel if rel)
        all_w = sum(w for w, _ in sel)
        out.append({"threshold": t, "precision": tp / all_w if all_w else None,
                    "recall": tp / total_rel if total_rel else None})
    return out


# --- the blind card ---------------------------------------------------------

# The ONLY fields a labelling card may carry. No score, rationale, state, source/channel,
# item id or history: the label must not be able to depend on what the scorer said.
CARD_FIELDS = ("sample_id", "position", "total", "wave", "title", "company", "seniority", "stack",
               "location", "remote", "relocation", "salary", "snippet", "link", "label", "chips")


def card_payload(*, sample_id: int, position: int, total: int, wave: int, extracted: Dict[str, Any],
                 text: str, link: Optional[str], label: Optional[str] = None,
                 chips: Optional[List[str]] = None) -> Dict[str, Any]:
    ex = extracted or {}
    sal = None
    lo, hi, cur = ex.get("salary_min"), ex.get("salary_max"), ex.get("currency")
    if lo or hi:
        sal = f"{lo or ''}–{hi or ''} {cur or ''}".strip(" –")
    snippet = " ".join((text or "").split())
    return {
        "sample_id": sample_id, "position": position, "total": total, "wave": wave,
        "title": ex.get("title"), "company": ex.get("company"), "seniority": ex.get("seniority"),
        "stack": ex.get("stack") or [], "location": ex.get("location"), "remote": ex.get("remote"),
        "relocation": ex.get("relocation"), "salary": sal,
        "snippet": snippet[:600] + ("…" if len(snippet) > 600 else ""),
        "link": link, "label": label, "chips": chips or [],
    }
