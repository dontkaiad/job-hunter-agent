#!/usr/bin/env python3
"""Eval: compare Haiku scores against stored Sonnet scores on production vacancies.

Run BEFORE deploying the cost-routing change to verify that Haiku scores agree
closely enough with Sonnet scores that no good vacancy would be incorrectly
rejected (false-reject) and no bad one incorrectly surfaced (false-surface).

Usage (from repo root, with prod DATABASE_URL and ANTHROPIC_API_KEY in .env):

    .venv/bin/python scripts/eval_score_routing.py

Prints a table: vacancy_id | sonnet_score | haiku_score | diff
Then: MAE, false-reject count (Sonnet>=60, Haiku<60), false-surface count.
"""

from __future__ import annotations

import os
import sys
import json
import time

# Load .env from repo root before importing any project code.
from pathlib import Path
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)

# Repo root on path so `job_hunter` is importable.
sys.path.insert(0, str(Path(__file__).parent.parent))

from job_hunter import store
from job_hunter.llm import LLMClient, llm_score, CHEAP_MODEL
from job_hunter.scoring import SURFACE_THRESHOLD, clamp_score
from job_hunter.pipeline import _load_extracted

MAX_ITEMS = 30          # cap: avoid a very long / expensive eval run
THROTTLE_S = 0.5        # pause between calls to avoid rate-limit bursts


def main() -> None:
    db_url = os.environ.get("DATABASE_URL") or ""
    api_key = os.environ.get("ANTHROPIC_API_KEY") or ""
    if not db_url:
        sys.exit("DATABASE_URL not set — add it to .env or export it")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY not set — add it to .env or export it")

    haiku_model = os.environ.get("ANTHROPIC_CHEAP_MODEL") or CHEAP_MODEL
    print(f"[eval] DB={db_url[:40]}...")
    print(f"[eval] Haiku model={haiku_model!r}  threshold={SURFACE_THRESHOLD}")

    conn = store.connect(db_url)

    # Load profile for the rubric (same as production).
    profile = None
    try:
        from job_hunter.llm import load_profile
        profile = load_profile()
        print(f"[eval] profile loaded")
    except Exception as exc:
        print(f"[eval] profile not loaded ({exc!r}), using generic rubric")

    # Fetch vacancies with a stored Sonnet score and raw_text.
    # Include all non-discovered states so we get a mix of surfaced/rejected.
    rows = conn.execute(
        """
        SELECT id, raw_text, relevance_score, state
        FROM work_items
        WHERE relevance_score IS NOT NULL
          AND raw_text IS NOT NULL
          AND raw_text <> ''
          AND state NOT IN ('discovered')
        ORDER BY updated_at DESC
        LIMIT %s
        """,
        (MAX_ITEMS,),
    ).fetchall()

    if not rows:
        print("[eval] No scored vacancies found in the DB — nothing to compare.")
        return

    print(f"\n[eval] Scoring {len(rows)} vacancies with Haiku...")
    print(f"\n{'id':>8}  {'sonnet':>7}  {'haiku':>7}  {'diff':>6}  state")
    print("-" * 50)

    client = LLMClient(api_key=api_key, model=haiku_model)

    results = []
    for row in rows:
        item_id = row["id"]
        sonnet_score = int(round(float(row["relevance_score"])))
        state = row["state"]
        raw_text = row["raw_text"] or ""

        # Re-create the ExtractResult from stored extracted_json.
        item = store.get_item(conn, item_id)
        extracted = _load_extracted(item) if item else None
        if extracted is None:
            print(f"{'':>8}  skip: id={item_id} — no extracted_json")
            continue

        try:
            verdict = llm_score(client, extracted, raw_text, model=haiku_model, profile=profile)
            haiku_score = clamp_score(verdict["score"])
        except Exception as exc:
            print(f"{item_id:>8}  ERROR: {exc!r}")
            continue

        diff = haiku_score - sonnet_score
        results.append({"id": item_id, "sonnet": sonnet_score, "haiku": haiku_score, "diff": diff, "state": state})
        print(f"{item_id:>8}  {sonnet_score:>7}  {haiku_score:>7}  {diff:>+6}  {state}")
        time.sleep(THROTTLE_S)

    if not results:
        print("\n[eval] No results produced.")
        return

    n = len(results)
    mae = sum(abs(r["diff"]) for r in results) / n

    # False-reject: Sonnet would have surfaced it (>=60) but Haiku would reject (<60).
    false_reject = [r for r in results if r["sonnet"] >= SURFACE_THRESHOLD and r["haiku"] < SURFACE_THRESHOLD]
    # False-surface: Haiku would surface but Sonnet scored below threshold.
    false_surface = [r for r in results if r["sonnet"] < SURFACE_THRESHOLD and r["haiku"] >= SURFACE_THRESHOLD]

    print("\n" + "=" * 50)
    print(f"Vacancies compared : {n}")
    print(f"Mean absolute error: {mae:.1f} pts")
    print(f"False-reject       : {len(false_reject)}  (Sonnet>=60, Haiku<60 — good vacancy missed)")
    if false_reject:
        for r in false_reject:
            print(f"  id={r['id']} sonnet={r['sonnet']} haiku={r['haiku']} state={r['state']}")
    print(f"False-surface      : {len(false_surface)}  (Sonnet<60, Haiku>=60 — weak vacancy shown)")
    if false_surface:
        for r in false_surface:
            print(f"  id={r['id']} sonnet={r['sonnet']} haiku={r['haiku']} state={r['state']}")

    print()
    if len(false_reject) == 0 and mae < 10:
        print("✅ Haiku scores are close enough — safe to deploy cost routing.")
    elif len(false_reject) <= 1 and mae < 15:
        print("⚠️  Minor divergence — check false-rejects before deploying.")
    else:
        print("🔴 Significant divergence — review prompt or raise threshold before deploying.")

    conn.close()


if __name__ == "__main__":
    main()
