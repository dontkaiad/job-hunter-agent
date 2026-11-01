"""Canonical form of a vacancy URL, used to spot the same vacancy posted twice.

The same vacancy often arrives as several posts (different channels / sources), each
with its own ``source_link`` (a Telegram permalink is unique per post) but the same
link to the real vacancy in the body. ``canonical_url`` turns that link into a
comparison key. PURE, never raises.

Normalised: scheme (http == https), host case, leading ``www.``, default port,
fragment, trailing slash, known tracking query parameters, query parameter order.
NOT normalised on purpose: the PATH case (slugs on teletype and most job boards are
case-sensitive, lowercasing would merge different vacancies) and any query parameter
that is not on the tracking list (``?id=1`` vs ``?id=2`` are different vacancies).

A URL that identifies a site or a company rather than ONE vacancy has no key and must
never merge unrelated posts: a bare site root, a "careers" / "jobs" / "vacancies" index
page, and every link from a source whose posts carry a company-level link with the
vacancy text inline (``hn_hiring``). Checked on the 2026-11-01 DB: 30 of the 103
duplicate groups involved ``hn_hiring`` and 17 pointed at generic careers pages, where
a merge would silently drop a genuinely different role.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

from . import vacancy_fetch

# Exact-name tracking parameters (``utm_*`` is matched by prefix).
TRACKING_PARAMS = frozenset(
    {"fbclid", "gclid", "yclid", "ref", "ref_src", "mc_cid", "mc_eid", "igshid"}
)


# Last path segment of a company / index page (``/careers``, ``/about/careers``, ``/jobs``).
# ``/jobs/123`` ends in a specific id and is NOT affected.
# ``/job?id=7`` (singular + id) is a specific vacancy and is NOT in this set; a
# ``/vacancies?rp=1`` query on an index page is a filter, not an id, so it stays out.
INDEX_SEGMENTS = frozenset({
    "career", "careers", "jobs", "vacancies", "openings", "positions",
    "join", "join-us", "hiring", "work-with-us",
})
# Sources whose link is the company's, not the vacancy's (text is inline in the post).
NO_DEDUP_SOURCES = frozenset({"hn_hiring"})


def _is_tracking(name: str) -> bool:
    n = name.lower()
    return n.startswith("utm_") or n in TRACKING_PARAMS


def canonical_url(url: Optional[str]) -> Optional[str]:
    """Comparison key for ``url``, or None if it is not a dedup-able vacancy URL."""
    try:
        parts = urlsplit((url or "").strip())
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
        host = parts.hostname.lower().rstrip(".")
        if host.startswith("www."):
            host = host[4:]
        port = parts.port
        netloc = host if port in (None, 80, 443) else f"{host}:{port}"
        path = parts.path or ""
        if path.endswith("/"):
            path = path.rstrip("/")
        query = urlencode(
            sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if not _is_tracking(k))
        )
        if not path and not query:
            return None
        last = path.rsplit("/", 1)[-1].lower()
        if last in INDEX_SEGMENTS:
            return None
        return f"https://{netloc}{path}" + (f"?{query}" if query else "")
    except Exception:
        return None


def primary_url_key(
    raw_text: Optional[str],
    source_link: Optional[str] = None,
    source_channel: Optional[str] = None,
) -> Optional[str]:
    """Key of the post's main vacancy link (same pick as the fetcher uses)."""
    if source_channel in NO_DEDUP_SOURCES:
        return None
    return canonical_url(vacancy_fetch.pick_url(raw_text, source_link))
