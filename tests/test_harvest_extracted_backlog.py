"""B3: a harvest looks at the WHOLE parked backlog in EXTRACTED, not just the oldest 100.

The parked vacancies (`insufficient` / `link_only`) stay in EXTRACTED and their `updated_at` does not
move, so with a hard limit of 100 the same 100 were picked every harvest and the rest never got their fetch.
"""

from __future__ import annotations

from job_hunter import run, store
from job_hunter.states import EXTRACTED, SURFACED


def _extracted(conn, n, base):
    ids = []
    for i in range(n):
        item_id = store.insert_item(conn, raw_text="stub", source_channel="ch", source_message_id=f"{base}-{i}")
        store.update_state(conn, item_id, EXTRACTED, from_state="discovered", kind="deterministic",
                           actor="system", reason="seed", extracted_json="{}")
        ids.append(item_id)
    return ids


def test_all_extracted_beyond_100_are_picked(conn):
    ids = _extracted(conn, 130, "b3-all")
    picked = run.items_to_process(conn, [])
    assert set(ids) <= picked, f"{len(set(ids) - picked)} extracted items starved"


def test_new_ids_are_included_and_surfaced_backlog_is_not(conn):
    surfaced = store.insert_item(conn, raw_text="s", source_channel="ch", source_message_id="b3-surf")
    store.update_state(conn, surfaced, SURFACED, from_state="discovered", kind="deterministic", actor="system",
                       reason="seed", extracted_json="{}", relevance_score=80.0)
    picked = run.items_to_process(conn, [999_999_001])
    assert 999_999_001 in picked
    assert surfaced not in picked          # no re-notification of the standing SURFACED backlog


def test_other_states_keep_the_default_limit_and_the_extracted_cap_is_generous():
    assert run.EXTRACTED_BACKLOG_LIMIT >= 500
