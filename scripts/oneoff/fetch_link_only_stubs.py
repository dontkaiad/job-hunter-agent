"""ONE-OFF: fetch the real text behind the accumulated link-only stubs.

Targets work_items in state 'extracted' (not score_failed) that are parked
(defer_reason link_only/insufficient) or look like link-only stubs. Denylisted
hosts (config FETCH_DENYLIST) are never requested.

  dry-run (default): fetches each page and scores it with the production path
      (Haiku, temperature 0, forced tool) to show what WOULD happen. Writes NOTHING
      to the DB. Costs one small Haiku call per fetched stub.
  --apply: persists ONLY the fetch result (fetched_text / fetch_status). It does
      not score or delete anything -- the 10:00 harvest scores them and delivers
      the surfaced ones through the normal notify path.

Run inside the job-hunter container:

    PYTHONPATH=/app python scripts/oneoff/fetch_link_only_stubs.py            # dry-run
    PYTHONPATH=/app python scripts/oneoff/fetch_link_only_stubs.py --apply
"""
from __future__ import annotations

import collections
import json
import sys
import time

from job_hunter import llm, scoring, store, vacancy_fetch
from job_hunter.config import load_config
from job_hunter.profile import load_profile
from job_hunter.schema_extract import from_dict

PAUSE_S = 1.0


def main(argv) -> int:
    apply = "--apply" in argv
    cfg = load_config()
    conn = store.connect(cfg.database_url)
    store.init_db(conn)
    client = profile = None
    if not apply:
        client = llm.AnthropicClient(cfg.anthropic_api_key, cfg.cheap_model)
        profile = load_profile()

    items = [
        it for it in store.list_by_state(conn, "extracted", include_score_failed=False)
        if it.defer_reason in ("link_only", "insufficient")
        or scoring.is_link_only(it.raw_text, cfg.link_only_min_chars)
    ]
    print(f"{len(items)} candidate stubs; denylist={cfg.fetch_denylist}")
    by_host = collections.defaultdict(collections.Counter)
    for it in items:
        if it.fetched_text:
            print(f"id={it.id} already fetched, skipped")
            continue
        res = vacancy_fetch.fetch_vacancy_text(
            it.raw_text, it.source_link, denylist=cfg.fetch_denylist
        )
        host = res.host or "-"
        verdict = ""
        if not apply and res.status == vacancy_fetch.OK:
            try:
                ex = from_dict(json.loads(it.extracted_json))
                v = llm.llm_score(
                    client, ex, it.raw_text or "", model=cfg.cheap_model,
                    profile=profile, fetched_text=res.text,
                )
                verdict = f"score={v['score']}" + (
                    f" (<{cfg.min_persist_score}: would be deleted)"
                    if v["score"] < cfg.min_persist_score else ""
                )
            except llm.InsufficientContent:
                verdict = "insufficient_content (stays parked)"
            except Exception as exc:  # noqa: BLE001
                verdict = f"score error {type(exc).__name__}"
        label = res.status + (f" / {verdict.split()[0]}" if verdict else "")
        by_host[host][label.split("=")[0]] += 1
        print(f"id={it.id} host={host} fetch={res.status} chars={len(res.text)} {verdict}")
        if apply:
            store.record_fetch(
                conn, it.id, res.status, text=res.text or None, url=res.url,
                count_attempt=res.status == vacancy_fetch.FAILED,
            )
        if res.status in (vacancy_fetch.OK, vacancy_fetch.FAILED):
            time.sleep(PAUSE_S)
    print("\nby host:")
    for host, c in sorted(by_host.items()):
        print(f"  {host}: {dict(c)}")
    print("applied" if apply else "dry-run (nothing written; pass --apply to persist fetches)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
