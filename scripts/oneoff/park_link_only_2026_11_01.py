"""ONE-OFF: repair the 2026-11-01 scoring incident (link-only stubs burned attempts).

Nine vacancies (ids below) sat at score_attempts=2 of 3 because Haiku answered
link-only stub posts in prose. This resets their attempts (so tomorrow's harvest
does not push them into score_failed) and parks the ones that really are link-only
stubs with defer_reason='link_only'. A genuine long post (11009) only gets its
attempts reset. Idempotent. Dry-run unless --apply.

Run inside the job-hunter container AFTER the deploy that adds the defer_reason
column / scoring.is_link_only:

    python scripts/oneoff/park_link_only_2026_11_01.py            # dry-run
    python scripts/oneoff/park_link_only_2026_11_01.py --apply
"""
from __future__ import annotations

import sys

from job_hunter import scoring, store
from job_hunter.config import load_config

IDS = [11009, 11063, 11085, 11154, 11211, 11345, 11349, 11351, 11353]


def main(argv) -> int:
    apply = "--apply" in argv
    cfg = load_config()
    conn = store.connect(cfg.database_url)
    store.init_db(conn)  # ensures the defer_reason column exists
    for item_id in IDS:
        item = store.get_item(conn, item_id)
        if item is None:
            print(f"id={item_id}: missing, skipped")
            continue
        stub = item.state == "extracted" and scoring.is_link_only(
            item.raw_text, cfg.link_only_min_chars
        )
        plan = "park link_only + reset attempts" if stub else "reset attempts only"
        print(
            f"id={item_id} state={item.state} attempts={item.score_attempts} "
            f"failed={item.score_failed} defer={item.defer_reason} "
            f"substantive={scoring.substantive_text_len(item.raw_text)} -> {plan}"
        )
        if apply and item.state == "extracted":
            store.set_defer_reason(
                conn, item_id, "link_only" if stub else item.defer_reason,
                audit_reason="oneoff 2026-11-01: reset attempts"
                + ("; parked link_only" if stub else ""),
                reset_attempts=True,
            )
    print("applied" if apply else "dry-run (pass --apply to write)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
