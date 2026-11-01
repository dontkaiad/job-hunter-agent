"""ONE-OFF: fill work_items.primary_url_key for rows that predate URL-based dedup.

New posts are compared against EVERY existing row (any state), so already-seen
vacancies need their key too -- otherwise a re-post of something already sent or
skipped would not be recognised.

  dry-run (default): reports how many rows get a key and which groups share one
      (the cross-channel duplicates), by state. Writes nothing.
  --apply: sets primary_url_key where it is NULL (one transaction). Deletes and
      scores nothing -- duplicates still in 'extracted' are dropped by the normal
      pipeline at the next harvest.

    PYTHONPATH=/app python scripts/oneoff/backfill_url_keys.py            # dry-run
    PYTHONPATH=/app python scripts/oneoff/backfill_url_keys.py --apply
"""
from __future__ import annotations

import collections
import sys
from typing import Dict, List, Tuple

from job_hunter import store, vacancy_url
from job_hunter.config import load_config


def plan(rows) -> Tuple[Dict[int, str], Dict[str, List[Tuple[int, str]]]]:
    """(id -> new key for rows lacking one, key -> [(id, state)] over ALL rows). PURE."""
    new_keys: Dict[int, str] = {}
    groups: Dict[str, List[Tuple[int, str]]] = collections.defaultdict(list)
    for r in rows:
        key = r["primary_url_key"]
        if key is None:
            key = vacancy_url.primary_url_key(r["raw_text"], r["source_link"], r.get("source_channel"))
            if key:
                new_keys[r["id"]] = key
        if key:
            groups[key].append((r["id"], r["state"]))
    return new_keys, groups


def main(argv) -> int:
    apply = "--apply" in argv
    cfg = load_config()
    conn = store.connect(cfg.database_url)
    store.init_db(conn)  # adds the columns / indexes if missing
    rows = conn.execute(
        "SELECT id, state, raw_text, source_link, source_channel, primary_url_key FROM work_items"
    ).fetchall()
    new_keys, groups = plan(rows)
    dup_groups = {k: sorted(v) for k, v in groups.items() if len(v) > 1}
    would_drop = [
        (i, st) for members in dup_groups.values() for i, st in members[1:]
        if st == "extracted"
    ]
    print(f"rows: {len(rows)}; need a key: {sum(1 for r in rows if r['primary_url_key'] is None)}; "
          f"get a key now: {len(new_keys)}; no vacancy link / not dedup-able: "
          f"{sum(1 for r in rows if r['primary_url_key'] is None) - len(new_keys)}")
    print(f"duplicate groups (same vacancy URL): {len(dup_groups)}; "
          f"extra copies: {sum(len(m) - 1 for m in dup_groups.values())}")
    print("group sizes:", dict(collections.Counter(len(m) for m in dup_groups.values())))
    print("states of extra copies:",
          dict(collections.Counter(st for m in dup_groups.values() for _, st in m[1:])))
    print(f"still in 'extracted' (dropped as duplicates at the next harvest): {len(would_drop)} "
          f"ids={[i for i, _ in would_drop]}")
    if apply:
        with conn.transaction():
            for item_id, key in new_keys.items():
                conn.execute("UPDATE work_items SET primary_url_key = %s WHERE id = %s", (key, item_id))
        print(f"applied: {len(new_keys)} keys set")
    else:
        print("dry-run (nothing written; pass --apply)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
