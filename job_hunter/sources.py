"""Ingest Source abstraction + the multi-source registry.

The minimal generalization from the hh/jobicy recon: every ingest source is a
``Source`` that knows how to fetch its listings, watermark them, and store new
``work_items`` — all producing the SAME internal item shape (``IngestMessage``
-> ``store.insert_item``). The deterministic extract->score->advance pipeline is
unchanged: a Source only feeds the front of it.

Two concrete sources today:
  - ``TelegramWebSource`` — the public t.me/s/ web reader. A thin adapter that
    delegates to ``ingest_web`` VERBATIM (per-channel watermark, no behaviour
    change). It "becomes a Source" without any rewrite of the proven path.
  - ``JobicySource`` — the first international-remote auto-source. Sweeps a small
    set of EU geo slugs against the no-auth Jobicy JSON API, mapping each vacancy
    to an ``IngestMessage``. PLAIN GET, zero LLM at ingest.

Shared watermark/store logic (``_store_with_watermark``) is the exact per-cursor
body factored out of ``ingest_web.ingest`` — reused VERBATIM by Jobicy so the
incremental cursor + dedup behave identically to the Telegram path.

WATERMARK vs DEDUP (the Jobicy subtlety): the channel_state watermark is keyed
by an arbitrary string and is INDEPENDENT of ``work_items.source_channel``. So
Jobicy watermarks per geo (key ``"jobicy:<geo>"``) while every job carries the
SAME ``source_channel="jobicy"``. The partial unique index on
(source_channel, source_message_id) therefore dedups a job that appears under
both ``europe`` and ``germany`` sweeps to a single card.
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Protocol, Tuple

import httpx

from . import ingest_web, store
from .clock import now_utc
from .config import Config
from .ingest_telegram import IngestMessage, store_messages
from .ingest_web import filter_new_messages, ingestion_cutoff, parse_post_datetime
from .research_fetch import html_to_text


class Source(Protocol):
    """An ingest source. ``ingest`` stores new items and returns their ids.

    ``ingest_with_own_connection`` is the thread-entry variant: ``run.ingest``
    offloads synchronous (httpx) sources to a worker thread, and each worker
    owns its OWN psycopg connection end-to-end (never crossing the main-thread
    connection over the thread boundary), mirroring ``ingest_web``.
    """

    name: str

    def ingest(self, cfg: Config, conn) -> List[int]:
        ...

    def ingest_with_own_connection(self, cfg: Config) -> List[int]:
        ...


def _store_with_watermark(
    conn,
    cursor_key: str,
    messages: List[IngestMessage],
    *,
    now,
    lookback_days: int,
) -> List[int]:
    """Watermark + cutoff + filter + store for ONE cursor. Returns new ids.

    This is the per-channel body of ``ingest_web.ingest`` factored out so any
    source reuses the identical incremental logic:
      - NEW cursor (no watermark) -> ingest the last ``lookback_days`` days.
      - existing cursor -> ingest only messages strictly newer than the watermark.
      - advance the watermark to the newest post SEEN (all messages, not just the
        fresh ones), so it tracks the cursor head even when nothing was new.
    Dedup is still enforced by ``store.insert_item`` (the partial unique index),
    so a message that slips past the date filter is never double-inserted.
    """
    watermark = store.get_channel_watermark(conn, cursor_key)
    cutoff = ingestion_cutoff(watermark, now, lookback_days)
    fresh = filter_new_messages(messages, cutoff)
    new_ids = store_messages(conn, fresh)

    dated = [m.posted_at for m in messages if m.posted_at is not None]
    if dated:
        store.set_channel_watermark(conn, cursor_key, max(dated))
    return new_ids


# --- Telegram web source (adapter over the existing ingest_web path) ---------


class TelegramWebSource:
    """The public t.me/s/ web reader as a Source. Delegates to ``ingest_web``
    VERBATIM so the per-channel watermark / dedup behaviour is unchanged."""

    name = "telegram-web"

    def ingest(self, cfg: Config, conn) -> List[int]:
        return ingest_web.ingest(cfg, conn)

    def ingest_with_own_connection(self, cfg: Config) -> List[int]:
        # Identical to today's call in run.ingest: own connection in the worker
        # thread, opened+closed inside ingest_web.
        return ingest_web.ingest_with_own_connection(cfg)


# --- Jobicy international-remote source --------------------------------------

JOBICY_API = "https://jobicy.com/api/v2/remote-jobs"

# Constant source_channel for EVERY geo so (source_channel, source_message_id)
# dedups the same job across overlapping geo sweeps. The per-geo cursor lives in
# channel_state under a SEPARATE key ("jobicy:<geo>"), decoupled from this.
JOBICY_SOURCE = "jobicy"

# Polite spacing between geo GETs. Jobicy's API notice asks for only a few
# fetches/day and warns that excessive requests may be throttled; the sweep is a
# handful of GETs, spaced by this delay.
JOBICY_REQUEST_DELAY = 1.0

# A browser-ish UA so the plain endpoint serves us reliably.
_USER_AGENT = "Mozilla/5.0 (compatible; job-hunter-agent/1.0; +https://jobicy.com)"


def _jobicy_url(geo: str, *, industry: str, count: int) -> str:
    """Build the Jobicy listing URL for one geo. PURE."""
    return f"{JOBICY_API}?count={int(count)}&industry={industry}&geo={geo}"


def job_to_message(job: dict, geo: str) -> Optional[IngestMessage]:
    """Map ONE Jobicy job object to an IngestMessage. PURE, never raises.

    raw_text = title + company + a compact remote/geo/level/type header +
    html_to_text(jobDescription) [the FULL description, NOT the 219-char
    jobExcerpt] + the apply URL. ``source_channel`` is the CONSTANT
    ``"jobicy"`` (dedup across geos); ``source_message_id`` is the stable
    ``"jobicy:<id>"``. Returns None when the id or all text is missing.
    """
    try:
        jid = job.get("id")
        if jid is None:
            return None
        title = (job.get("jobTitle") or "").strip()
        company = (job.get("companyName") or "").strip()
        body = html_to_text(job.get("jobDescription") or "")

        meta_bits = ["Remote"]
        if job.get("jobGeo"):
            meta_bits.append(str(job["jobGeo"]).strip())
        if job.get("jobLevel"):
            meta_bits.append(str(job["jobLevel"]).strip())
        job_type = job.get("jobType")
        if isinstance(job_type, list) and job_type:
            meta_bits.append("/".join(str(t) for t in job_type))
        elif isinstance(job_type, str) and job_type.strip():
            meta_bits.append(job_type.strip())

        lines: List[str] = []
        if title:
            lines.append(title)
        if company:
            lines.append(f"Company: {company}")
        lines.append(" · ".join(meta_bits))
        if body:
            lines.append("")
            lines.append(body)
        url = job.get("url")
        if url:
            lines.append("")
            lines.append(str(url))

        raw_text = "\n".join(lines).strip()
        if not raw_text:
            return None

        return IngestMessage(
            source_channel=JOBICY_SOURCE,
            source_message_id=f"jobicy:{jid}",
            source_link=str(url) if url else None,
            raw_text=raw_text,
            posted_at=parse_post_datetime(job.get("pubDate")),
        )
    except Exception:
        return None


class JobicySource:
    """Jobicy no-auth JSON API as a Source. Sweeps ``cfg.jobicy_geos``.

    ``http_get`` is injectable for tests (no network). ``request_delay`` spaces
    the geo GETs; tests pass 0 to avoid sleeping.
    """

    name = "jobicy"

    def __init__(
        self,
        http_get: Optional[Callable[[str], "httpx.Response"]] = None,
        request_delay: float = JOBICY_REQUEST_DELAY,
    ) -> None:
        self._http_get = http_get
        self._request_delay = request_delay

    def fetch(self, cfg: Config, geo: str) -> List[IngestMessage]:
        """Fetch + map ONE geo's listings to IngestMessages. Network I/O.

        PLAIN GET, no auth, zero LLM. Returns [] on any HTTP/JSON error so one
        bad geo never aborts the sweep.
        """
        get = self._http_get or _default_get
        url = _jobicy_url(geo, industry=cfg.jobicy_industry, count=cfg.jobicy_count)
        resp = get(url)
        resp.raise_for_status()
        data = resp.json()
        jobs = data.get("jobs") or []
        out: List[IngestMessage] = []
        for job in jobs:
            msg = job_to_message(job, geo)
            if msg is not None:
                out.append(msg)
        return out

    def ingest(self, cfg: Config, conn) -> List[int]:
        """Sweep every configured geo, watermark per geo, store new items.

        Each geo uses its OWN cursor ("jobicy:<geo>"); all jobs share
        source_channel="jobicy" so the same job under two geos dedups to one row.
        """
        if not cfg.jobicy_geos:
            return []
        now = now_utc()
        new_ids: List[int] = []
        for i, geo in enumerate(cfg.jobicy_geos):
            if i > 0 and self._request_delay > 0:
                time.sleep(self._request_delay)  # polite spacing between GETs
            try:
                msgs = self.fetch(cfg, geo)
            except Exception as exc:  # noqa: BLE001 - keep sweeping other geos
                print(f"[ingest-jobicy] geo {geo} failed: {exc}")
                continue
            ids = _store_with_watermark(
                conn, f"{JOBICY_SOURCE}:{geo}", msgs,
                now=now, lookback_days=cfg.new_channel_lookback_days,
            )
            print(f"[ingest-jobicy] geo {geo}: {len(msgs)} read, {len(ids)} new")
            new_ids.extend(ids)
        return new_ids

    def ingest_with_own_connection(self, cfg: Config) -> List[int]:
        """Open a fresh connection, ingest, close — all in the CALLING thread."""
        conn = store.connect(cfg.database_url)
        try:
            store.init_db(conn)
            return self.ingest(cfg, conn)
        finally:
            conn.close()


def _default_get(url: str) -> "httpx.Response":
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    return httpx.get(url, timeout=20.0, follow_redirects=True, headers=headers)


# --- Registry / aggregator ---------------------------------------------------


def http_sources(cfg: Config, mode: str = "web") -> List[Source]:
    """The ordered list of SYNCHRONOUS (httpx) sources for this run.

    - telegram-web unless INGEST_MODE=telethon (that path is async and handled
      directly by run.ingest against the main-thread connection),
    - Jobicy when JOBICY_GEOS is non-empty (opt-in).
    Each is offloaded to a worker thread by run.ingest via
    ``ingest_with_own_connection``.
    """
    sources: List[Source] = []
    if (mode or "web").lower() != "telethon":
        sources.append(TelegramWebSource())
    if cfg.jobicy_geos:
        sources.append(JobicySource())
    return sources


def aggregate(cfg: Config, conn, sources: List[Source]) -> List[int]:
    """Run each source over ``conn`` and concatenate the new ids. SYNCHRONOUS.

    Used in tests to verify multi-source fan-out; run.ingest offloads each
    source to a thread instead so the event loop is not blocked.
    """
    new_ids: List[int] = []
    for source in sources:
        new_ids.extend(source.ingest(cfg, conn))
    return new_ids
