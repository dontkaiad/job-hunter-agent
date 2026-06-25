"""Eval: compare Haiku scores against stored Sonnet scores on production vacancies.

Run INSIDE the job-hunter container where env vars and the profile are already
present — no manual .env sourcing needed:

    docker compose exec job-hunter python -m job_hunter.eval_score_routing

Reads config the same way the bot does (load_config → DATABASE_URL +
ANTHROPIC_API_KEY). Fetches a STRATIFIED sample across score bands so the
critical threshold region (40-75) is always covered, then re-scores with Haiku
and prints a full diagnostic table.

Metrics:
  MAE          — mean absolute error (magnitude; blind to direction)
  BIAS         — signed mean diff (haiku - sonnet); positive = Haiku over-scores
  Distribution — how many haiku > / < / = sonnet (detects systematic shift)
  Corridor     — items where sonnet OR haiku is in [CORRIDOR_LO, CORRIDOR_HI];
                 threshold errors hurt most here
  Agreement    — fraction of items where both models make the same surface/reject
                 decision; this is the business metric that matters

Verdict (green ✅) requires ALL of:
  |bias| < 8  AND  corridor false-reject = 0  AND  corridor false-surface = 0
Otherwise 🔴 with a diagnosis.
"""

from __future__ import annotations

import sys
import time

from . import store
from .config import load_config
from .llm import AnthropicClient, llm_score
from .pipeline import _load_extracted
from .profile import load_profile
from .scoring import SURFACE_THRESHOLD, clamp_score

MAX_PER_BAND = 8      # items per score band → up to 32 total
THROTTLE_S   = 0.4
CORRIDOR_LO  = 50
CORRIDOR_HI  = 70


_BAND_QUERY = """
SELECT id, raw_text, relevance_score, state
FROM work_items
WHERE relevance_score IS NOT NULL
  AND raw_text IS NOT NULL AND raw_text <> ''
  AND state NOT IN ('discovered', 'extracted')
  AND relevance_score >= %s AND relevance_score < %s
ORDER BY updated_at DESC
LIMIT %s
"""


def _fetch_stratified(conn, per_band: int):
    """Return up to 4*per_band rows spread across score bands."""
    bands = [(0, 40), (40, 60), (60, 75), (75, 101)]
    rows = []
    seen_ids: set = set()
    for lo, hi in bands:
        band_rows = conn.execute(_BAND_QUERY, (lo, hi, per_band)).fetchall()
        for r in band_rows:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                rows.append(r)
    return rows


def main() -> int:
    cfg = load_config()
    if not cfg.database_url:
        print("[eval] ERROR: DATABASE_URL not set in environment", file=sys.stderr)
        return 1
    if not cfg.anthropic_api_key:
        print("[eval] ERROR: ANTHROPIC_API_KEY not set in environment", file=sys.stderr)
        return 1

    haiku_model = cfg.cheap_model
    print(f"[eval] haiku_model={haiku_model!r}  threshold={SURFACE_THRESHOLD}")
    print(f"[eval] corridor=[{CORRIDOR_LO},{CORRIDOR_HI}]  sampling={MAX_PER_BAND}/band\n")

    profile = load_profile()

    conn = store.connect(cfg.database_url)
    store.init_db(conn)

    rows = _fetch_stratified(conn, MAX_PER_BAND)

    if not rows:
        print("[eval] No scored vacancies found — nothing to compare.")
        conn.close()
        return 0

    print(f"[eval] Comparing {len(rows)} vacancies (stratified) with Haiku vs stored scores...\n")
    print(f"{'id':>8}  {'stored':>7}  {'haiku':>7}  {'diff':>6}  {'in_corr':>7}  state")
    print("-" * 62)

    client = AnthropicClient(api_key=cfg.anthropic_api_key, model=haiku_model)
    results = []

    for row in rows:
        item_id = row["id"]
        sonnet_score = int(round(float(row["relevance_score"])))
        state = row["state"]
        in_corridor = CORRIDOR_LO <= sonnet_score <= CORRIDOR_HI

        item = store.get_item(conn, item_id)
        extracted = _load_extracted(item) if item else None
        if extracted is None:
            print(f"{item_id:>8}  skip: no extracted_json")
            continue

        try:
            verdict = llm_score(
                client, extracted, item.raw_text or "",
                model=haiku_model, profile=profile,
            )
            haiku_score = clamp_score(verdict["score"])
        except Exception as exc:
            print(f"{item_id:>8}  ERROR: {exc!r}")
            continue

        diff = haiku_score - sonnet_score
        in_corridor_any = CORRIDOR_LO <= sonnet_score <= CORRIDOR_HI or CORRIDOR_LO <= haiku_score <= CORRIDOR_HI
        results.append({
            "id": item_id, "sonnet": sonnet_score,
            "haiku": haiku_score, "diff": diff, "state": state,
            "in_corridor": in_corridor_any,
        })

        flag = ""
        if sonnet_score >= SURFACE_THRESHOLD and haiku_score < SURFACE_THRESHOLD:
            flag = "  ← FALSE-REJECT"
        elif sonnet_score < SURFACE_THRESHOLD and haiku_score >= SURFACE_THRESHOLD:
            flag = "  ← false-surface"

        corr_mark = "  *" if in_corridor_any else ""
        print(f"{item_id:>8}  {sonnet_score:>7}  {haiku_score:>7}  {diff:>+6}  {str(in_corridor_any):>7}  {state}{flag}{corr_mark}")
        time.sleep(THROTTLE_S)

    conn.close()

    if not results:
        print("\n[eval] No results produced.")
        return 0

    n = len(results)
    mae  = sum(abs(r["diff"]) for r in results) / n
    bias = sum(r["diff"] for r in results) / n

    above = sum(1 for r in results if r["diff"] > 0)
    below = sum(1 for r in results if r["diff"] < 0)
    equal = sum(1 for r in results if r["diff"] == 0)

    agree_surface = [r for r in results if r["sonnet"] >= SURFACE_THRESHOLD and r["haiku"] >= SURFACE_THRESHOLD]
    agree_reject  = [r for r in results if r["sonnet"] <  SURFACE_THRESHOLD and r["haiku"] <  SURFACE_THRESHOLD]
    false_reject  = [r for r in results if r["sonnet"] >= SURFACE_THRESHOLD and r["haiku"] <  SURFACE_THRESHOLD]
    false_surface = [r for r in results if r["sonnet"] <  SURFACE_THRESHOLD and r["haiku"] >= SURFACE_THRESHOLD]
    agreement_rate = (len(agree_surface) + len(agree_reject)) / n * 100

    corridor      = [r for r in results if r["in_corridor"]]
    corr_fr       = [r for r in corridor if r["sonnet"] >= SURFACE_THRESHOLD and r["haiku"] <  SURFACE_THRESHOLD]
    corr_fs       = [r for r in corridor if r["sonnet"] <  SURFACE_THRESHOLD and r["haiku"] >= SURFACE_THRESHOLD]

    print("\n" + "=" * 62)
    print(f"Vacancies compared : {n}")
    print()
    print(f"MAE                : {mae:.1f} pts   (magnitude; blind to direction)")
    print(f"BIAS               : {bias:+.1f} pts  (signed mean; + = Haiku over-scores)")
    print(f"Distribution       : Haiku higher={above}  lower={below}  equal={equal}")
    print()
    print(f"Agreement rate     : {agreement_rate:.0f}%  ({n} items)")
    print(f"  agree-surface    : {len(agree_surface)}")
    print(f"  agree-reject     : {len(agree_reject)}")
    print(f"  false-surface    : {len(false_surface)}  (stored<{SURFACE_THRESHOLD}, Haiku>={SURFACE_THRESHOLD})")
    print(f"  false-reject     : {len(false_reject)}  (stored>={SURFACE_THRESHOLD}, Haiku<{SURFACE_THRESHOLD})")
    print()
    print(f"Corridor [{CORRIDOR_LO}-{CORRIDOR_HI}]     : {len(corridor)} items  (* in table above)")
    print(f"  corridor false-reject  : {len(corr_fr)}")
    print(f"  corridor false-surface : {len(corr_fs)}")

    bias_ok  = abs(bias) < 8
    corr_ok  = len(corr_fr) == 0 and len(corr_fs) == 0
    ok       = bias_ok and corr_ok

    print()
    if ok:
        print("✅ Haiku scores are close enough — safe to keep cost routing.")
    else:
        print("🔴 Divergence too large — review before next harvest.")
        if not bias_ok:
            direction = "over-scores" if bias > 0 else "under-scores"
            print(f"   BIAS ({bias:+.1f}) outside ±8: Haiku systematically {direction}")
        if not corr_ok:
            if corr_fr:
                print(f"   corridor false-rejects ({len(corr_fr)}): good vacancies near threshold missed by Haiku")
            if corr_fs:
                print(f"   corridor false-surfaces ({len(corr_fs)}): low-quality vacancies near threshold surfaced by Haiku")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
