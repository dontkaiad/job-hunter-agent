"""Eval: compare Haiku scores against stored Sonnet scores on production vacancies.

Run INSIDE the job-hunter container where env vars and the profile are already
present — no manual .env sourcing needed:

    docker compose exec job-hunter python -m job_hunter.eval_score_routing

Reads config the same way the bot does (load_config → DATABASE_URL +
ANTHROPIC_API_KEY). Takes up to MAX_ITEMS already-scored vacancies, re-scores
them with Haiku, and prints:

    id | sonnet_score | haiku_score | diff | state

Then: MAE, false-reject count (Sonnet>=60 but Haiku<60), false-surface count.
Exit 1 when MAE>15 or false-reject>1 so CI can gate on it.
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

MAX_ITEMS = 30
THROTTLE_S = 0.4


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

    profile = load_profile()
    print(f"[eval] profile loaded: {profile.name!r}")

    conn = store.connect(cfg.database_url)
    store.init_db(conn)

    rows = conn.execute(
        """
        SELECT id, raw_text, relevance_score, state
        FROM work_items
        WHERE relevance_score IS NOT NULL
          AND raw_text IS NOT NULL AND raw_text <> ''
          AND state NOT IN ('discovered', 'extracted')
        ORDER BY updated_at DESC
        LIMIT %s
        """,
        (MAX_ITEMS,),
    ).fetchall()

    if not rows:
        print("[eval] No scored vacancies found — nothing to compare.")
        conn.close()
        return 0

    print(f"[eval] Comparing {len(rows)} vacancies with Haiku vs stored Sonnet scores...\n")
    print(f"{'id':>8}  {'sonnet':>7}  {'haiku':>7}  {'diff':>6}  state")
    print("-" * 52)

    client = AnthropicClient(api_key=cfg.anthropic_api_key, model=haiku_model)
    results = []

    for row in rows:
        item_id = row["id"]
        sonnet_score = int(round(float(row["relevance_score"])))
        state = row["state"]

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
        results.append({
            "id": item_id, "sonnet": sonnet_score,
            "haiku": haiku_score, "diff": diff, "state": state,
        })
        flag = ""
        if sonnet_score >= SURFACE_THRESHOLD and haiku_score < SURFACE_THRESHOLD:
            flag = "  ← FALSE-REJECT"
        elif sonnet_score < SURFACE_THRESHOLD and haiku_score >= SURFACE_THRESHOLD:
            flag = "  ← false-surface"
        print(f"{item_id:>8}  {sonnet_score:>7}  {haiku_score:>7}  {diff:>+6}  {state}{flag}")
        time.sleep(THROTTLE_S)

    conn.close()

    if not results:
        print("\n[eval] No results produced.")
        return 0

    n = len(results)
    mae = sum(abs(r["diff"]) for r in results) / n
    false_reject = [r for r in results if r["sonnet"] >= SURFACE_THRESHOLD and r["haiku"] < SURFACE_THRESHOLD]
    false_surface = [r for r in results if r["sonnet"] < SURFACE_THRESHOLD and r["haiku"] >= SURFACE_THRESHOLD]

    print("\n" + "=" * 52)
    print(f"Vacancies compared : {n}")
    print(f"Mean absolute error: {mae:.1f} pts")
    print(f"False-reject       : {len(false_reject)}  (Sonnet>={SURFACE_THRESHOLD}, Haiku<{SURFACE_THRESHOLD})")
    print(f"False-surface      : {len(false_surface)}  (Sonnet<{SURFACE_THRESHOLD}, Haiku>={SURFACE_THRESHOLD})")

    ok = len(false_reject) <= 1 and mae <= 15
    print()
    if ok:
        print("✅ Haiku scores are close enough — safe to keep cost routing.")
    else:
        print("🔴 Divergence too large — review results before next harvest.")
        if len(false_reject) > 1:
            print(f"   false-rejects ({len(false_reject)}) > 1: good vacancies would be missed by Haiku")
        if mae > 15:
            print(f"   MAE ({mae:.1f}) > 15: scores drift too much on average")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
