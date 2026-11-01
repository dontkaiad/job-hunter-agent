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

A URL with no path and no meaningful query (a site root, a landing page) has no key:
it identifies a site, not a vacancy, and must never merge unrelated posts.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

from . import vacancy_fetch

# Exact-name tracking parameters (``utm_*`` is matched by prefix).
TRACKING_PARAMS = frozenset(
    {"fbclid", "gclid", "yclid", "ref", "ref_src", "mc_cid", "mc_eid", "igshid"}
)


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
        return f"https://{netloc}{path}" + (f"?{query}" if query else "")
    except Exception:
        return None


def primary_url_key(raw_text: Optional[str], source_link: Optional[str] = None) -> Optional[str]:
    """Key of the post's main vacancy link (same pick as the fetcher uses)."""
    return canonical_url(vacancy_fetch.pick_url(raw_text, source_link))
