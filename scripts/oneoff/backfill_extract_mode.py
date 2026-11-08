"""ONE-OFF: fill work_items.extract_mode for rows that predate the extract-mode marker.

Derived from each row's extraction audit line (state_transitions 'extract (...)').
NO API calls: database only. Dry-run unless --apply. Idempotent; touches only
extract_mode, and only where it is NULL.

    PYTHONPATH=/app python scripts/oneoff/backfill_extract_mode.py            # dry-run
    PYTHONPATH=/app python scripts/oneoff/backfill_extract_mode.py --apply
"""
from __future__ import annotations

import collections
import re
import sys
from typing import Optional

from job_hunter import store
from job_hunter.config import load_config


def mode_from_reason(reason: Optional[str]) -> Optional[str]:
    """'extract (llm+reconcile)' -> 'llm', 'extract (heuristic(fallback))' ->
    'heuristic:fallback', ... ; None when the line is missing or unknown. PURE."""
    m = re.match(r"^extract \((.*)\)$", reason or "")
    if not m:
        return None
    v = m.group(1)
    if v.startswith("llm"):
        return "llm"
    if v == "heuristic(fallback)":
        return "heuristic:fallback"
    if v.startswith("heuristic(topic-filtered"):
        return "heuristic:topic"
    if v.startswith("heuristic(prefilter"):
        return "heuristic:prefilter"
    if v.startswith("heuristic(duplicate"):
        return "heuristic:duplicate"
    return None


def main(argv) -> int:
    apply = "--apply" in argv
    cfg = load_config()
    conn = store.connect(cfg.database_url)
    store.init_db(conn)  # adds the columns if missing
    rows = conn.execute(
        "SELECT w.id, w.state, (SELECT t.reason FROM state_transitions t WHERE t.item_id = w.id "
        "AND t.reason LIKE 'extract (%%' ORDER BY t.id DESC LIMIT 1) AS xr "
        "FROM work_items w WHERE w.extract_mode IS NULL").fetchall()
    todo = [(r["id"], mode_from_reason(r["xr"])) for r in rows]
    known = [(i, m) for i, m in todo if m]
    print(f"rows without extract_mode: {len(rows)}; derivable: {len(known)}; unknown (left NULL): {len(rows) - len(known)}")
    print("derived modes:", dict(collections.Counter(m for _, m in known)))
    fb = [r for r, (_, m) in zip(rows, todo) if m == "heuristic:fallback"]
    print("legacy fallback by state:", dict(collections.Counter(r["state"] for r in fb)))
    if apply:
        for item_id, m in known:
            conn.execute("UPDATE work_items SET extract_mode = %s WHERE id = %s AND extract_mode IS NULL", (m, item_id))
        conn.commit()
        print(f"applied: {len(known)} rows marked (no API call was made)")
    else:
        print("dry-run (nothing written; pass --apply)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
