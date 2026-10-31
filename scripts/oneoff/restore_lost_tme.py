"""ONE-OFF: re-add t.me vacancies lost in the 2026-10-20..28 Anthropic credit outage.

The scoring failure turned every vacancy into score=0 and deleted it. Their
URLs were saved from the container logs; this re-reads those posts from the
public t.me/s/ reader and pushes them through the normal pipeline.

NOT part of the pipeline. Run inside the job-hunter container:

    python scripts/oneoff/restore_lost_tme.py /path/to/urls.txt

Behaviour:
  - Groups URLs (https://t.me/<channel>/<id>) by channel; each t.me/s/<channel>
    ?before=<id> page carries ~20 posts, so one request serves many ids.
  - Polite: 2 s pause between requests; stops after 3 channels in a row that hit
    HTTP 429 / an empty page (the regular harvest shares this IP).
  - Inserts through ingest_telegram.store_messages with the SAME source_channel /
    source_message_id the regular ingest uses (dedup works; channel_state
    watermarks are NOT touched), then pipeline.run_to_gate.
  - Aborts if scoring starts failing (credits/auth, or 5 deferred in a row).
"""
from __future__ import annotations

import collections
import re
import sys
import time
from typing import Dict, List, Optional, Set

import httpx

from job_hunter import ingest_web, pipeline, store
from job_hunter.bot import build_deps
from job_hunter.config import load_config
from job_hunter.ingest_telegram import IngestMessage, store_messages

PAUSE_S = 2.0
MAX_PAGES_PER_CHANNEL = 10
MAX_BAD_CHANNELS_IN_ROW = 3
_URL_RE = re.compile(r"^https?://t\.me/(?:s/)?([A-Za-z0-9_]+)/(\d+)")


class RateLimited(Exception):
    pass


def _get(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; job-hunter-agent/1.0)"}
    resp = httpx.get(url, timeout=20.0, follow_redirects=True, headers=headers)
    if resp.status_code == 429:
        raise RateLimited(url)
    resp.raise_for_status()
    return resp.text


def find_posts(channel: str, wanted: Set[int], stats: collections.Counter) -> Dict[int, IngestMessage]:
    """Walk t.me/s/<channel>?before=N pages downward until every wanted id is found."""
    found: Dict[int, IngestMessage] = {}
    before = max(wanted) + 1
    for page in range(MAX_PAGES_PER_CHANNEL):
        url = f"{ingest_web.public_channel_url(channel)}?before={before}"
        html = _get(url)  # RateLimited / HTTPError propagate to the caller
        stats["requests"] += 1
        time.sleep(PAUSE_S)
        msgs = ingest_web.parse_channel_html(html, f"@{channel}")
        if not msgs:
            if page == 0:
                raise RuntimeError("empty page")
            break
        ids = [int(m.source_message_id) for m in msgs]
        for m in msgs:
            if int(m.source_message_id) in wanted:
                found[int(m.source_message_id)] = m
        if wanted <= set(found) or min(ids) <= min(wanted):
            break
        before = min(ids)
    return found


def main() -> None:
    urls = [u.strip() for u in open(sys.argv[1]) if u.strip()]
    by_channel: Dict[str, Set[int]] = collections.defaultdict(set)
    for u in urls:
        m = _URL_RE.match(u)
        if m:
            by_channel[m.group(1)].add(int(m.group(2)))
    print(f"{len(urls)} urls, {len(by_channel)} channels", flush=True)

    cfg = load_config()
    conn = store.connect(cfg.database_url)
    deps = build_deps(cfg)
    calls: collections.Counter = collections.Counter()
    orig = deps.llm_client.complete

    def counted(system, user, **kw):
        calls[kw.get("model") or "default"] += 1
        return orig(system, user, **kw)

    deps.llm_client.complete = counted

    tally: collections.Counter = collections.Counter()
    http: collections.Counter = collections.Counter()
    by_ch: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    bad_in_row = 0
    consecutive_deferred = 0
    aborted: Optional[str] = None

    for channel, wanted in sorted(by_channel.items()):
        try:
            found = find_posts(channel, set(wanted), http)
            bad_in_row = 0
        except (RateLimited, RuntimeError, httpx.HTTPError) as exc:
            bad_in_row += 1
            tally["channel_unreadable"] += 1
            tally["dead_or_unfetched"] += len(wanted)
            by_ch[channel]["dead_or_unfetched"] += len(wanted)
            print(f"[{channel}] unreadable: {type(exc).__name__}", flush=True)
            if bad_in_row >= MAX_BAD_CHANNELS_IN_ROW:
                aborted = f"{bad_in_row} channels in a row returned 429/empty/errors"
                break
            time.sleep(PAUSE_S)
            continue

        for mid in sorted(wanted):
            msg = found.get(mid)
            if msg is None:
                tally["dead_post"] += 1
                by_ch[channel]["dead_post"] += 1
                continue
            if store.find_duplicate_by_source_link(conn, msg.source_link, -1) is not None:
                tally["skipped_already_in_db"] += 1
                by_ch[channel]["skipped_already_in_db"] += 1
                continue
            ids = store_messages(conn, [msg])
            if not ids:
                tally["skipped_already_in_db"] += 1
                by_ch[channel]["skipped_already_in_db"] += 1
                continue
            for item_id in ids:
                pipeline.run_to_gate(conn, item_id, deps=deps)
                item = store.get_item(conn, item_id)
                if item is None:
                    key = "dropped_low_score"
                elif item.state == "extracted":
                    key = "deferred_scoring_failed"
                elif item.state == "surfaced":
                    key = "surfaced_ge60"
                else:
                    key = f"kept_{item.state}"
                tally[key] += 1
                by_ch[channel][key] += 1
                consecutive_deferred = consecutive_deferred + 1 if key == "deferred_scoring_failed" else 0
            if consecutive_deferred >= 5 or deps.score_stats.kinds.get(pipeline.FAIL_FATAL):
                aborted = "scoring is failing (credits/auth or 5 deferred in a row)"
                break
        print(f"[{channel}] {dict(by_ch[channel])}", flush=True)
        if aborted:
            break

    print("ABORT:" if aborted else "DONE", aborted or "", dict(tally), flush=True)
    print("t.me requests:", http["requests"], "| llm calls:", dict(calls),
          "| score failures:", deps.score_stats.failed, dict(deps.score_stats.kinds), flush=True)
    conn.close()


if __name__ == "__main__":
    main()
