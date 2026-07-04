#!/usr/bin/env python3
"""Re-score work_items from the last 14 days with the updated STRONG FIT rubric.

Results are written to a separate `rescoring_runs` table so the old scores
remain untouched. Kai can inspect the diff before deciding whether to promote
any item to `surfaced`.

Usage (run from repo root):
    python scripts/rescore_recent.py [--days 14] [--dry-run]

Requires DATABASE_URL and ANTHROPIC_API_KEY in the environment (or .env file).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure the job_hunter package is importable when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg
from psycopg.rows import dict_row

from job_hunter.clock import now_iso
from job_hunter.config import load_config
from job_hunter.llm import AnthropicClient, llm_score
from job_hunter.profile import load_profile
from job_hunter.schema_extract import ExtractResult, from_dict

TERMINAL_STATES = ("scored", "rejected", "surfaced", "skipped")

DDL_RESCORING = """
CREATE TABLE IF NOT EXISTS rescoring_runs (
    id            SERIAL PRIMARY KEY,
    run_id        TEXT        NOT NULL,
    item_id       INTEGER     NOT NULL REFERENCES work_items(id),
    old_score     REAL,
    new_score     REAL,
    score_delta   REAL,
    new_reasoning TEXT,
    error         TEXT,
    created_at    TEXT        NOT NULL
);
CREATE INDEX IF NOT EXISTS rescoring_runs_run_id_idx ON rescoring_runs(run_id);
CREATE INDEX IF NOT EXISTS rescoring_runs_item_id_idx ON rescoring_runs(item_id);
"""


def _ensure_rescoring_table(conn: psycopg.Connection) -> None:
    for stmt in DDL_RESCORING.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    conn.commit()


def _fetch_recent_items(conn: psycopg.Connection, days: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, state, source_channel, source_link, raw_text,
               extracted_json, relevance_score, created_at
        FROM work_items
        WHERE state = ANY(%s)
          AND created_at >= (now() AT TIME ZONE 'UTC' - make_interval(days => %s))::text
        ORDER BY created_at DESC
        """,
        (list(TERMINAL_STATES), days),
    ).fetchall()
    return [dict(r) for r in rows]


def _extracted_from_row(row: dict) -> Optional[ExtractResult]:
    if not row.get("extracted_json"):
        return None
    try:
        data = json.loads(row["extracted_json"])
        return from_dict(data)
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-score recent work_items")
    parser.add_argument("--days", type=int, default=14, help="Look-back window in days")
    parser.add_argument("--dry-run", action="store_true", help="Score but do not write to DB")
    args = parser.parse_args()

    cfg = load_config()
    if not cfg.database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    if not cfg.anthropic_api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    profile = load_profile()
    client = AnthropicClient(api_key=cfg.anthropic_api_key, model=cfg.judge_model)

    run_id = f"rescoring_run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    print(f"Run ID: {run_id}")
    print(f"Look-back: {args.days} days | dry-run: {args.dry_run}")

    conn = psycopg.connect(cfg.database_url, row_factory=dict_row)
    try:
        if not args.dry_run:
            _ensure_rescoring_table(conn)

        items = _fetch_recent_items(conn, args.days)
        print(f"Found {len(items)} items in terminal states from the last {args.days} days")

        results = []
        errors = 0

        for i, row in enumerate(items, 1):
            item_id = row["id"]
            old_score = row.get("relevance_score")
            raw_text = row.get("raw_text") or ""

            extracted = _extracted_from_row(row)
            if extracted is None:
                # No extracted_json → skip (can't score without extraction)
                print(f"  [{i}/{len(items)}] #{item_id} SKIP (no extracted_json)")
                if not args.dry_run:
                    ts = now_iso()
                    conn.execute(
                        """
                        INSERT INTO rescoring_runs
                            (run_id, item_id, old_score, new_score, score_delta,
                             new_reasoning, error, created_at)
                        VALUES (%s, %s, %s, NULL, NULL, NULL, %s, %s)
                        """,
                        (run_id, item_id, old_score, "no extracted_json", ts),
                    )
                    conn.commit()
                continue

            title = extracted.title or "(no title)"
            company = extracted.company or ""
            label = f"{title}" + (f" @ {company}" if company else "")

            try:
                result = llm_score(client, extracted, raw_text, profile=profile)
                new_score = result.get("score")
                new_reasoning = result.get("reasoning", "")
                delta = (new_score - old_score) if (new_score is not None and old_score is not None) else None
                delta_str = f"{delta:+.1f}" if delta is not None else "N/A"
                print(f"  [{i}/{len(items)}] #{item_id} {label} | {old_score} → {new_score} ({delta_str})")
                results.append({
                    "item_id": item_id,
                    "title": title,
                    "company": company,
                    "old_score": old_score,
                    "new_score": new_score,
                    "delta": delta,
                    "new_reasoning": new_reasoning,
                })
                if not args.dry_run:
                    ts = now_iso()
                    conn.execute(
                        """
                        INSERT INTO rescoring_runs
                            (run_id, item_id, old_score, new_score, score_delta,
                             new_reasoning, error, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, NULL, %s)
                        """,
                        (run_id, item_id, old_score, new_score, delta, new_reasoning, ts),
                    )
                    conn.commit()
            except Exception as exc:
                errors += 1
                print(f"  [{i}/{len(items)}] #{item_id} ERROR: {exc}")
                if not args.dry_run:
                    ts = now_iso()
                    conn.execute(
                        """
                        INSERT INTO rescoring_runs
                            (run_id, item_id, old_score, new_score, score_delta,
                             new_reasoning, error, created_at)
                        VALUES (%s, %s, %s, NULL, NULL, NULL, %s, %s)
                        """,
                        (run_id, item_id, old_score, traceback.format_exc()[:2000], ts),
                    )
                    conn.commit()

    finally:
        conn.close()

    # --- Report -----------------------------------------------------------------
    scored_results = [r for r in results if r["delta"] is not None]
    jumped = [r for r in scored_results if r["delta"] >= 10]
    jumped_sorted = sorted(jumped, key=lambda r: r["delta"], reverse=True)

    print("\n" + "=" * 60)
    print("RESCORE REPORT")
    print("=" * 60)
    print(f"Total items found:       {len(items)}")
    print(f"Successfully re-scored:  {len(results)}")
    print(f"Errors / skipped:        {errors + (len(items) - len(results) - errors)}")
    print(f"Score changed ≥+10:      {len(jumped)}")
    if not args.dry_run:
        print(f"Results written to:      rescoring_runs (run_id={run_id})")

    if jumped_sorted:
        print(f"\nTop-{min(10, len(jumped_sorted))} by score increase (≥+10):")
        for rank, r in enumerate(jumped_sorted[:10], 1):
            print(
                f"  {rank:2d}. #{r['item_id']:6d}  {r['old_score']:5.1f} → {r['new_score']:5.1f}"
                f"  (+{r['delta']:.1f})  {r['title'][:50]}"
                + (f" @ {r['company'][:25]}" if r['company'] else "")
            )
    else:
        print("\nNo items gained ≥10 points.")

    print("\nTo compare old vs new scores in psql:")
    print(f"  SELECT wi.id, wi.relevance_score AS old, rr.new_score,")
    print(f"         rr.score_delta, wi.state,")
    print(f"         (wi.extracted_json::json->>'title') AS title")
    print(f"  FROM rescoring_runs rr")
    print(f"  JOIN work_items wi ON wi.id = rr.item_id")
    print(f"  WHERE rr.run_id = '{run_id}'")
    print(f"  ORDER BY rr.score_delta DESC NULLS LAST;")


if __name__ == "__main__":
    main()
