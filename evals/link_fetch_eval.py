"""evals/link_fetch_eval.py -- recovering link-only stubs by fetching the page behind the link.

Question: of the link-only / insufficient stubs, how many yield usable page text and
what do they score once the text is there?
Method: for each candidate stub, fetch its link with the production fetcher
(vacancy_fetch: SSRF guard, denylist, 8 s timeout) and score the page with the
production scorer (Haiku, temperature 0, forced tool). No DB writes.

  offline -- synthetic pages (evals/synthetic/pages.json): chrome stripping for the
             two special-cased hosts, size cap, denylist (no request is made),
             Telegram-only link -> no_url, unusable pages -> failed. No network.
  run     -- live: python -m evals.link_fetch_eval run [--ids ...]
             Network fetches + one Haiku call per fetched page (~$0.003 each).
             Default candidates: extracted, not score_failed, parked or link-only.
             Prints per-host status and score buckets only (no page text).

Measures whether the TEXT could be recovered, not whether the resulting score is right.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import time
from typing import Any, Dict, List, Optional

from evals.common import SYNTHETIC, host_of, load_json, report


class _Resp:
    def __init__(self, status=200, text="", headers=None):
        self.status_code, self.text = status, text
        self.headers = headers or {"content-type": "text/html"}


def run_offline() -> Dict[str, Any]:
    from job_hunter import research_fetch as rf, vacancy_fetch as vf

    pages = load_json(SYNTHETIC / "pages.json")
    real_safe = rf._url_is_safe
    rf._url_is_safe = lambda u: True          # no DNS in the offline run
    out: Dict[str, Any] = {}
    try:
        calls: List[str] = []

        def get_for(html, status=200, ctype="text/html"):
            def get(url, **kw):
                calls.append(url)
                return _Resp(status, html, {"content-type": ctype})
            return get

        tt = vf.fetch_vacancy_text(pages["teletype_post"], get=get_for(pages["teletype_html"]))
        rm = vf.fetch_vacancy_text(pages["remocate_post"], get=get_for(pages["remocate_html"]))
        out["teletype_ok_chrome_stripped"] = (
            tt.status == vf.OK and "About Teletype" not in tt.text and "views" not in tt.text)
        out["remocate_ok_chrome_stripped"] = (
            rm.status == vf.OK and "Popular Countries" not in rm.text)
        capped = vf.fetch_vacancy_text(pages["teletype_post"], get=get_for(pages["teletype_html"] * 30), max_chars=1000)
        out["size_cap_ok"] = capped.status == vf.OK and len(capped.text) <= 1000
        calls.clear()
        denied = [vf.fetch_vacancy_text(p, get=get_for(pages["teletype_html"]))
                  for p in pages["denied_posts"]]
        out["denylisted_all"] = all(d.status == vf.DENYLISTED for d in denied)
        out["denylisted_made_no_request"] = calls == []
        out["telegram_only_is_no_url"] = vf.fetch_vacancy_text(
            "https://t.me/chan/1", "https://t.me/chan/1", get=get_for("x")).status == vf.NO_URL
        bad = [vf.fetch_vacancy_text(pages["teletype_post"], get=get_for("<html>tiny</html>")),
               vf.fetch_vacancy_text(pages["teletype_post"], get=get_for("e", status=404)),
               vf.fetch_vacancy_text(pages["teletype_post"], get=get_for("%PDF", ctype="application/pdf"))]
        out["unusable_pages_fail_cleanly"] = all(b.status == vf.FAILED for b in bad)
    finally:
        rf._url_is_safe = real_safe
    return out


def run_live(ids: Optional[List[int]]) -> Dict[str, Any]:
    from evals.common import live_env
    from job_hunter import llm, scoring, store, vacancy_fetch as vf
    from job_hunter.schema_extract import from_dict

    cfg, profile, client, conn = live_env()
    rows = conn.execute(
        "SELECT id, raw_text, extracted_json, source_link, defer_reason, state, score_failed "
        "FROM work_items WHERE state = 'extracted' AND NOT score_failed"
    ).fetchall()
    rows = [r for r in rows if (ids and r["id"] in ids) or (not ids and (
        r["defer_reason"] in ("link_only", "insufficient")
        or scoring.is_link_only(r["raw_text"], cfg.link_only_min_chars)))]
    by_host: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    buckets: collections.Counter = collections.Counter()
    status_counts: collections.Counter = collections.Counter()
    page_hashes: collections.Counter = collections.Counter()
    for r in rows:
        res = vf.fetch_vacancy_text(r["raw_text"], r["source_link"], denylist=cfg.fetch_denylist)
        label = res.status
        status_counts[res.status] += 1
        if res.status == vf.OK:
            page_hashes[hashlib.sha1(res.text.encode()).hexdigest()] += 1
            try:
                s = llm.llm_score(client, from_dict(json.loads(r["extracted_json"])), r["raw_text"] or "",
                                  model=cfg.cheap_model, profile=profile, fetched_text=res.text)["score"]
                b = ("<%d (would be deleted)" % cfg.min_persist_score if s < cfg.min_persist_score
                     else "%d-59" % cfg.min_persist_score if s < scoring.SURFACE_THRESHOLD
                     else ">=%d (would surface)" % scoring.SURFACE_THRESHOLD)
            except llm.InsufficientContent:
                b = "insufficient_content (stays parked)"
            except Exception as exc:  # noqa: BLE001
                b = f"score error {type(exc).__name__}"
            buckets[b] += 1
            label = f"ok / {b}"
            time.sleep(1.0)
        by_host[res.host or host_of(r["source_link"])][label] += 1
    return {
        "candidates": len(rows),
        "fetch_status": dict(status_counts),
        "distinct_pages": len(page_hashes),
        "pages_seen_more_than_once": sum(1 for n in page_hashes.values() if n > 1),
        "score_buckets": dict(buckets),
        "by_host": {h: dict(c) for h, c in sorted(by_host.items())},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("offline")
    r = sub.add_parser("run")
    r.add_argument("--ids", type=int, nargs="*", default=None)
    a = ap.parse_args(argv)
    report("link-only fetch", run_offline() if a.cmd == "offline" else run_live(a.ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
