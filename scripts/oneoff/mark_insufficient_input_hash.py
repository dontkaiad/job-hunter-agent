"""ONE-OFF: stop the daily re-asking of vacancies already judged `insufficient`.

Before the input-hash rule, every harvest sent each `insufficient` vacancy to the model
again (nothing recorded what the model had read). This records, for each currently parked
`insufficient` vacancy, the hash of its CURRENT input (post text + fetched page text), so
from now on it is only sent again if that input changes. NO API calls: database only.
Dry-run unless --apply. Idempotent; touches only defer_input_hash.

    PYTHONPATH=/app python scripts/oneoff/mark_insufficient_input_hash.py            # dry-run
    PYTHONPATH=/app python scripts/oneoff/mark_insufficient_input_hash.py --apply
"""
from __future__ import annotations

import collections
import sys

from job_hunter import scoring, store
from job_hunter.config import load_config


def plan(rows):
    """[(id, new_hash)] for parked-insufficient rows whose stored hash is missing/stale. PURE."""
    out = []
    for r in rows:
        h = scoring.input_hash(r["raw_text"], r["fetched_text"])
        if r["defer_input_hash"] != h:
            out.append((r["id"], h))
    return out


def main(argv) -> int:
    apply = "--apply" in argv
    cfg = load_config()
    conn = store.connect(cfg.database_url)
    store.init_db(conn)  # adds the column if missing
    rows = conn.execute(
        "SELECT id, raw_text, fetched_text, defer_input_hash, source_channel FROM work_items "
        "WHERE state = 'extracted' AND defer_reason = 'insufficient'"
    ).fetchall()
    todo = plan(rows)
    print(f"parked as insufficient: {len(rows)}; need a hash: {len(todo)}; already marked: {len(rows) - len(todo)}")
    print("by source:", dict(collections.Counter(r["source_channel"] for r in rows).most_common(8)))
    if apply:
        for item_id, h in todo:
            store.set_input_hash(conn, item_id, h)
        print(f"applied: {len(todo)} hashes set (no API call was made)")
    else:
        print("dry-run (nothing written; pass --apply)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
