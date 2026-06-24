"""Shared "add a vacancy by URL" flow.

ONE code path, exposed TWO ways:
  - the dashboard ``POST /api/items/add`` endpoint (webapi.py), and
  - the Telegram bot's URL-message handler (bot.py).

The flow mirrors the manual-injection we did by hand, but productized:

    fetch (SSRF-guarded) -> build raw_text -> insert_item (source_channel="manual")
        -> run_to_gate (extract -> score -> surface/reject) -> read back

It reuses, verbatim:
  - ``research_fetch.fetch_research_context`` for the fetch + the SSRF guard +
    the HTML->text strip + the "too little usable text" threshold,
  - ``store.insert_item`` (and its (source_channel, source_message_id) dedup),
  - ``pipeline.run_to_gate`` -> ``advance()`` as the SOLE writer of state.

NO new scoring/pipeline logic and NO schema change: a manual item is just
another source feeding the identical contract, so the salary-floor guard and the
Sonnet rubric behave exactly as for a harvested Telegram card.

The fetch is INJECTABLE (``fetch=`` param) so tests exercise the flow with no
network; the default fetcher is the real SSRF-guarded ``research_fetch`` path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse, urlunparse

from . import pipeline as pipeline_mod
from . import research_fetch, store
from .pipeline import Deps

# All add-by-URL items share this source_channel. Dedup is on
# (source_channel, source_message_id) = ("manual", <url-derived key>).
MANUAL_SOURCE = "manual"

# Guard against absurd inputs before we touch the network or the DB.
_MAX_URL_LEN = 2000

# A manual item's fetched body is prefixed with the source URL on its own line so
# the apply link is reachable to the extractor's contact/link detection — the
# same trick the t.me/s/ reader uses to surface the apply URL into raw_text.


@dataclass
class AddOutcome:
    """Result of an add-by-URL attempt. ``status`` drives the caller's response.

    status:
      - "added"        -> a new item was inserted and run through the pipeline;
                          ``item_id`` / ``state`` / ``score`` are populated.
      - "duplicate"    -> this URL is already in the pipeline; the fields point at
                          the EXISTING item (no second card was created).
      - "invalid_url"  -> not a fetchable http(s) URL; nothing was inserted.
      - "unreadable"   -> fetched but too little usable text (JS-only / blocked /
                          non-HTML / SSRF-blocked); nothing was inserted.
    ``reason`` carries a short human message for the error statuses.
    """

    status: str
    item_id: Optional[int] = None
    state: Optional[str] = None
    score: Optional[float] = None
    reason: Optional[str] = None


def normalize_url(url: Optional[str]) -> Optional[str]:
    """Canonicalize a pasted URL for stable dedup. PURE, never raises.

    Lowercases scheme + host, drops the fragment, strips a trailing "/" from the
    path, and keeps the query as-is. Returns the normalized absolute URL, or None
    when the input is not an http(s) URL with a host. The query is preserved
    because vacancy ids frequently live there (e.g. ``?vacancyId=123``).
    """
    try:
        parsed = urlparse((url or "").strip())
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return None
        host = parsed.hostname.lower()
        netloc = f"{host}:{parsed.port}" if parsed.port else host
        path = parsed.path.rstrip("/")
        return urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))
    except Exception:
        return None


def manual_message_id(url: Optional[str]) -> Optional[str]:
    """Derive the dedup key (source_message_id) from a URL. PURE, never raises.

    Built from the normalized URL but WITHOUT the scheme, so http:// and https://
    variants of the same page dedup to one card. Returns None for a non-URL.
    """
    norm = normalize_url(url)
    if norm is None:
        return None
    p = urlparse(norm)
    key = f"{p.netloc}{p.path}"
    if p.query:
        key += f"?{p.query}"
    return key


def default_fetch(url: str) -> Optional[str]:
    """Fetch ``url`` and return its stripped page text, or None. NEVER raises.

    Delegates to ``research_fetch.fetch_research_context`` with ``max_urls=1`` so
    ONLY the pasted URL is fetched (no best-effort company second hop). That path
    enforces the SSRF guard (resolved-IP allowlist, DNS-rebind aware, redirect
    re-validation) and the ``_MIN_USABLE_CHARS`` (=200) "usable text" floor:
    anything below it — JS-only shells, blocked pages, non-HTML, SSRF-blocked
    hosts — comes back as zero pages, which we map to None ("unreadable").
    """
    ctx = research_fetch.fetch_research_context(url, max_urls=1)
    pages = ctx.get("pages") or []
    if not pages:
        return None
    return (pages[0].get("text") or "").strip() or None


def add_by_url(
    conn,
    url: str,
    deps: Deps,
    *,
    fetch: Callable[[str], Optional[str]] = default_fetch,
) -> AddOutcome:
    """Run the full add-by-URL flow and return an AddOutcome.

    Steps (each guarded):
      1. Validate the URL; derive the dedup key.
      2. Pre-check dedup: if the (manual, key) item already exists, return it.
      3. Fetch via the SSRF-guarded fetcher; empty -> "unreadable", no insert.
      4. insert_item (source_channel="manual", source_link=url, key). A lost race
         (concurrent insert) surfaces as a duplicate, not an error.
      5. run_to_gate -> extract -> score -> surface/reject. advance() stays the
         SOLE writer; we never write state here directly.
      6. Re-read the item and report its final state + score.
    """
    url = (url or "").strip()
    if not url or len(url) > _MAX_URL_LEN:
        return AddOutcome("invalid_url", reason="not a valid URL")

    key = manual_message_id(url)
    if key is None:
        return AddOutcome("invalid_url", reason="not a valid http(s) URL")

    existing = store.get_item_by_source(conn, MANUAL_SOURCE, key)
    if existing is not None:
        return AddOutcome(
            "duplicate",
            item_id=existing.id,
            state=existing.state,
            score=existing.relevance_score,
        )

    text = fetch(url)
    if not text:
        return AddOutcome("unreadable", reason="couldn't read that page")

    raw_text = f"{url}\n\n{text}"
    new_id = store.insert_item(
        conn,
        raw_text=raw_text,
        source_channel=MANUAL_SOURCE,
        source_link=url,
        source_message_id=key,
    )
    if new_id is None:
        # Lost the race to a concurrent add of the same URL: report the winner.
        existing = store.get_item_by_source(conn, MANUAL_SOURCE, key)
        if existing is not None:
            return AddOutcome(
                "duplicate",
                item_id=existing.id,
                state=existing.state,
                score=existing.relevance_score,
            )
        return AddOutcome("unreadable", reason="insert failed")

    # SAME pipeline as a harvested card: discovered -> extracted -> scored ->
    # surfaced/rejected, all through advance().
    pipeline_mod.run_to_gate(conn, new_id, deps=deps)

    item = store.get_item(conn, new_id)
    if item is None:  # pragma: no cover - just-inserted item cannot vanish
        return AddOutcome("unreadable", reason="item vanished after insert")
    return AddOutcome(
        "added", item_id=item.id, state=item.state, score=item.relevance_score
    )
