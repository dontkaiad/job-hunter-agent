"""Fetch the real vacancy text behind a link-only stub post (before scoring).

Reuses research_fetch's SSRF-guarded fetcher (resolved-IP checks, manual redirect
re-validation, 8s timeout, 1 redirect, HTML->text). NEVER raises: every failure is
a ``FetchResult`` with a status.

Statuses:
  ok          usable page text obtained
  denylisted  the target host is on the config denylist -> NO request is made
  no_url      the post has no fetchable (non-Telegram) link -> no request
  failed      request/parse failed or the page had no usable text
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlparse

from . import research_fetch as rf

OK = "ok"
DENYLISTED = "denylisted"
NO_URL = "no_url"
FAILED = "failed"

DEFAULT_DENYLIST = ("career.habr.com", "habr.com", "getmatch.ru", "linkedin.com")
DEFAULT_MAX_CHARS = 6000


@dataclass
class FetchResult:
    status: str
    url: Optional[str] = None
    host: Optional[str] = None
    text: str = ""


def host_denied(host: Optional[str], denylist: Sequence[str]) -> bool:
    """True if ``host`` equals a denylist entry or is its subdomain. PURE."""
    h = (host or "").strip().lower().rstrip(".")
    if h.startswith("www."):
        h = h[4:]
    if not h:
        return False
    for entry in denylist or ():
        e = (entry or "").strip().lower().lstrip(".")
        if e and (h == e or h.endswith("." + e)):
            return True
    return False


_TELETYPE_HEAD = re.compile(r"^\s*About Teletype Join\s+", re.I)
_TELETYPE_TAIL = re.compile(r"\s+\d+ views \d+ reactions.*$", re.S)
_REMOCATE_HEAD = re.compile(r"^\s*remocateJobsCountriesCompaniesBlogPost a Job\s+", re.I)
_REMOCATE_TAIL = re.compile(r"[→\s]*remocateLinkedIn\b.*$", re.S)


def clean_page_text(host: str, text: str) -> str:
    """Drop known site chrome (nav/footer) from stripped page text. PURE."""
    h = (host or "").lower()
    if h.endswith("teletype.in"):
        text = _TELETYPE_TAIL.sub("", _TELETYPE_HEAD.sub("", text))
    elif h.endswith("remocate.app"):
        text = _REMOCATE_TAIL.sub("", _REMOCATE_HEAD.sub("", text))
    return text.strip()


def pick_url(raw_text: Optional[str], source_link: Optional[str] = None) -> Optional[str]:
    """The link to fetch: first non-Telegram URL in the post, else a non-Telegram
    ``source_link`` (web sources whose permalink IS the vacancy). PURE."""
    url = rf.select_primary_url(None, raw_text)
    if url:
        return url
    link = (source_link or "").strip()
    p = urlparse(link)
    if p.scheme in ("http", "https") and p.hostname and not rf._is_telegram_host(p.hostname):
        return link
    return None


def fetch_vacancy_text(
    raw_text: Optional[str],
    source_link: Optional[str] = None,
    *,
    denylist: Sequence[str] = DEFAULT_DENYLIST,
    timeout: float = 8.0,
    max_chars: int = DEFAULT_MAX_CHARS,
    get: Optional[Callable[..., Any]] = None,
) -> FetchResult:
    """Resolve the stub's link to page text. NEVER raises."""
    try:
        url = pick_url(raw_text, source_link)
        if not url:
            return FetchResult(NO_URL)
        host = (urlparse(url).hostname or "").lower()
        if host_denied(host, denylist):
            return FetchResult(DENYLISTED, url=url, host=host)
        if not rf._url_is_safe(url):
            return FetchResult(FAILED, url=url, host=host)
        page = rf._fetch_one(
            url, get or rf._default_get,
            timeout=timeout, max_redirects=1, max_chars=max_chars * 2,
        )
        if page is None:
            return FetchResult(FAILED, url=url, host=host)
        final_host = (urlparse(page["url"]).hostname or host).lower()
        if host_denied(final_host, denylist):  # redirected onto a denied host
            return FetchResult(DENYLISTED, url=page["url"], host=final_host)
        text = clean_page_text(final_host, page["text"])[:max_chars]
        if len(text) < rf._MIN_USABLE_CHARS:
            return FetchResult(FAILED, url=page["url"], host=final_host)
        return FetchResult(OK, url=page["url"], host=final_host, text=text)
    except Exception:
        return FetchResult(FAILED)
