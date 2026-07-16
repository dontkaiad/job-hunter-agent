#!/usr/bin/env python3
"""Re-score recently-scored vacancies with the CURRENT rubric and diff against
the score already stored in the DB.

Pre-deploy safety gate for rubric changes (established after an earlier
incident where adding examples to STRONG FIT accidentally shifted the whole
rubric's calibration). Run this BEFORE deploying any change to
``_SCORE_SCAFFOLD`` in job_hunter/llm.py, on the branch/checkout that already
has the change, against the SAME production DB the change will run against:

    .venv/bin/python scripts/rescore_recent.py --days 14 --dry-run

``--dry-run`` NEVER writes to the DB — it only prints the diff table + a
summary verdict. Review the diff BEFORE deploying; only re-run without
``--dry-run`` (after the deploy) to persist the new scores:

    .venv/bin/python scripts/rescore_recent.py --days 14

The non-dry-run path updates ONLY ``work_items.relevance_score`` (never
``state`` — a rescore is not a pipeline transition) and appends an audit row
to ``state_transitions`` for traceability. It intentionally does NOT call
``store.update_state`` (reserved for ``pipeline.advance``, per its docstring).

STOP-THE-DEPLOY RULE (manual judgement, this script only supplies the data):
if more than half the compared vacancies moved by >= 10 points AND that shift
is NOT explained by a location-signal keyword hit (EOR/contractor mentions or
a "must be authorized to work" / "no sponsorship" clause), do not deploy the
rubric change — investigate first.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

# Load .env from repo root before importing any project code (same convention
# as scripts/eval_score_routing.py).
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)

# Repo root on path so `job_hunter` is importable.
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import timedelta

from job_hunter import store
from job_hunter.clock import now_iso, now_utc
from job_hunter.llm import AnthropicClient, JUDGE_MODEL, llm_score
from job_hunter.pipeline import _load_extracted
from job_hunter.profile import load_profile
from job_hunter.scoring import clamp_score

MAX_ITEMS = 200          # hard cap so a big --days window can't run away
THROTTLE_S = 0.4         # pause between calls to avoid rate-limit bursts
BIG_SHIFT_PTS = 10       # |diff| at/above this counts as a "big" shift

# Heuristic keyword probe (NOT the rubric logic itself — just used here to
# flag, for human review, whether a big shift plausibly correlates with the
# new ADDITIONAL LOCATION SIGNAL wording added to _SCORE_SCAFFOLD).
_LOCATION_SIGNAL_RE = re.compile(
    r"\b(deel|remote\.com|oyster|papaya global|contractor|freelance|"
    r"no visa sponsorship|worldwide|hire.{0,20}globally|"
    r"must be authorized to work|no sponsorship|w-?2 only)\b",
    re.I,
)


def _fetch_candidates(conn, days: int):
    """Rows updated within the last ``days``. updated_at is a TEXT column
    holding UTC ISO-8601 strings (see migrations/schema_pg.sql) — same-format
    strings sort lexicographically, so the cutoff is computed in Python
    (mirroring ``ingest_web.ingestion_cutoff``) rather than with SQL interval
    arithmetic against a text column.
    """
    cutoff = (now_utc() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT id, raw_text, relevance_score, state, updated_at
        FROM work_items
        WHERE relevance_score IS NOT NULL
          AND raw_text IS NOT NULL
          AND raw_text <> ''
          AND updated_at >= %s
        ORDER BY updated_at DESC
        LIMIT %s
        """,
        (cutoff, MAX_ITEMS),
    ).fetchall()
    return rows


def _apply_new_score(conn, item_id: int, state: str, new_score: float, old_score: float) -> None:
    """Persist ONLY the new relevance_score + an audit transition row.

    Deliberately does NOT go through ``store.update_state`` (reserved for
    ``pipeline.advance``) and deliberately does NOT change ``state`` — a
    rubric rescore reassesses fit, it is not a pipeline transition.
    """
    ts = now_iso()
    conn.execute(
        "UPDATE work_items SET relevance_score = %s, updated_at = %s WHERE id = %s",
        (new_score, ts, item_id),
    )
    conn.execute(
        """
        INSERT INTO state_transitions
            (item_id, from_state, to_state, kind, actor, reason, created_at)
        VALUES (%s, %s, %s, 'deterministic', 'system', %s, %s)
        """,
        (
            item_id, state, state,
            f"rescore: {old_score:.0f} -> {new_score:.0f} (rubric update)",
            ts,
        ),
    )
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14, help="lookback window (default 14)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the diff + verdict only; NEVER writes to the DB",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL") or ""
    api_key = os.environ.get("ANTHROPIC_API_KEY") or ""
    if not db_url:
        sys.exit("DATABASE_URL not set — add it to .env or export it")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY not set — add it to .env or export it")

    judge_model = os.environ.get("ANTHROPIC_JUDGE_MODEL") or JUDGE_MODEL
    mode = "DRY-RUN (read-only)" if args.dry_run else "APPLY (writes relevance_score)"
    print(f"[rescore] DB={db_url[:40]}...")
    print(f"[rescore] mode={mode}  days={args.days}  judge_model={judge_model!r}")

    conn = store.connect(db_url)

    try:
        profile = load_profile()
        print("[rescore] profile loaded")
    except Exception as exc:
        profile = None
        print(f"[rescore] profile load failed ({exc!r}), using generic rubric")

    rows = _fetch_candidates(conn, args.days)
    if not rows:
        print(f"[rescore] No scored vacancies in the last {args.days} days — nothing to do.")
        conn.close()
        return

    print(f"\n[rescore] Re-scoring {len(rows)} vacancies with the CURRENT rubric...")
    print(f"\n{'id':>8}  {'old':>5}  {'new':>5}  {'diff':>6}  {'loc?':>4}  state")
    print("-" * 55)

    client = AnthropicClient(api_key=api_key, model=judge_model)

    results = []
    for row in rows:
        item_id = row["id"]
        old_score = float(row["relevance_score"])
        state = row["state"]
        raw_text = row["raw_text"] or ""

        item = store.get_item(conn, item_id)
        extracted = _load_extracted(item) if item else None
        if extracted is None:
            print(f"{item_id:>8}  skip — no extracted_json")
            continue

        try:
            verdict = llm_score(client, extracted, raw_text, model=judge_model, profile=profile)
            new_score = float(clamp_score(verdict["score"]))
        except Exception as exc:
            print(f"{item_id:>8}  ERROR: {exc!r}")
            continue

        diff = new_score - old_score
        loc_hit = bool(_LOCATION_SIGNAL_RE.search(raw_text))
        results.append({
            "id": item_id, "old": old_score, "new": new_score, "diff": diff,
            "state": state, "loc_hit": loc_hit,
        })
        print(
            f"{item_id:>8}  {old_score:>5.0f}  {new_score:>5.0f}  {diff:>+6.0f}  "
            f"{'yes' if loc_hit else '  -':>4}  {state}"
        )
        time.sleep(THROTTLE_S)

    if not results:
        print("\n[rescore] No results produced.")
        conn.close()
        return

    n = len(results)
    big_shifts = [r for r in results if abs(r["diff"]) >= BIG_SHIFT_PTS]
    big_unexplained = [r for r in big_shifts if not r["loc_hit"]]

    print("\n" + "=" * 55)
    print(f"Vacancies compared        : {n}")
    print(f"Big shifts (|diff|>={BIG_SHIFT_PTS})   : {len(big_shifts)} ({100 * len(big_shifts) / n:.0f}%)")
    print(
        f"  ...of which unexplained  : {len(big_unexplained)} "
        f"(no Deel/EOR/contractor/no-sponsorship keyword in raw_text)"
    )
    if big_unexplained:
        for r in big_unexplained:
            print(f"    id={r['id']} old={r['old']:.0f} new={r['new']:.0f} diff={r['diff']:+.0f} state={r['state']}")

    print()
    half = n / 2.0
    if len(big_shifts) == 0:
        print("✅ No big shifts — safe to deploy.")
    elif len(big_unexplained) <= half and len(big_unexplained) < len(big_shifts):
        print(
            "⚠️  Some big shifts, but most correlate with a location-signal keyword "
            "— review the list above, then decide."
        )
    else:
        print(
            "🔴 STOP: more than half the big shifts have NO visible location-signal "
            "correlation — this looks like a calibration drift, not the intended "
            "location refinement. Do not deploy; investigate the rubric change."
        )

    if args.dry_run:
        print("\n[rescore] --dry-run: no DB writes performed.")
        conn.close()
        return

    print(f"\n[rescore] Applying {n} new scores to the DB...")
    for r in results:
        _apply_new_score(conn, r["id"], r["state"], r["new"], r["old"])
    print("[rescore] Done.")

    conn.close()


if __name__ == "__main__":
    main()
