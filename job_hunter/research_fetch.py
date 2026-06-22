"""Direct-URL research fetcher for the T10 research leg (Phase 1).

A self-contained, NEVER-RAISES helper that fetches the vacancy permalink (and,
best-effort, ONE company page) over plain synchronous HTTP, strips the HTML to
plain text with the stdlib ``html.parser`` (no new dependency), truncates each
page, and returns the usable page text.

I/O module. ANY failure — DNS / timeout / 4xx / 5xx / connection refused /
blocked / non-HTML body / empty-after-strip / JS-only-so-near-empty — yields an
EMPTY ``pages`` list and NEVER raises. The research path treats an empty result
as "no usable web content" and falls back to desk research, so the worst case is
exactly today's behavior.

Hard caps (defaults): at most 2 URLs total, 8s per-request timeout, at most 1
redirect followed, and each page truncated to ~6000 chars BEFORE it can reach
the LLM.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import httpx

# A normal browser-ish User-Agent so plain servers don't reject us outright.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 job-hunter-agent/1.0"
)

# Tags whose text content is never human-readable page copy.
_DROP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}

# Minimum stripped-text length to count a page as having usable content. Below
# this we treat the page as JS-only / empty / blocked and discard it.
_MIN_USABLE_CHARS = 200

# Content-types we accept as HTML/text. Anything else (PDF, images, JSON blobs)
# is discarded — we only know how to strip HTML.
_HTML_CONTENT_TYPES = ("text/html", "application/xhtml", "text/plain")


# --- SSRF GUARD -------------------------------------------------------------
#
# ``source_link`` originates from ingested Telegram posts (attacker-
# influenceable) and the discovered company-anchor URL is derived from a fetched
# page. A server-side GET of such a URL must not be allowed to reach internal
# infrastructure: cloud metadata (169.254.169.254), loopback (127.0.0.1 / ::1),
# RFC1918 (10/8, 172.16/12, 192.168/16), link-local (169.254/16, fe80::/10),
# unique-local (fc00::/7), etc. On the Vultr VPS the metadata endpoint can leak
# credentials, so we block by the RESOLVED IP (DNS-rebinding aware) rather than
# by hostname string.
#
# PINNING MECHANISM: we use "resolve-all-records + reject-if-any-private" plus
# MANUAL redirect revalidation, NOT true socket-level IP pinning. Cleanly
# pinning the connection to a validated IP while preserving correct SNI and TLS
# certificate verification against the original hostname is not feasible in
# httpx 0.27.x without a custom transport / significant complexity. Instead we
# resolve ALL A/AAAA records and reject the whole host if ANY record is private,
# and we re-resolve + re-validate every redirect target before following it.
#
# RESIDUAL: this leaves a sub-second TOCTOU window — between our getaddrinfo()
# validation and httpx's own resolution at connect time, a hostname's DNS could
# rebind to a private IP. The window is tiny, requires the attacker to control
# authoritative DNS with a ~0 TTL and win a race, and the hard, tested guarantee
# below still holds: a host whose resolution INCLUDES any private/loopback/
# link-local/reserved/multicast/unspecified IP is rejected outright.


def _ip_blocked(ip_str: str) -> bool:
    """True if ``ip_str`` is an address we must never fetch. PURE, never raises.

    Blocks private / loopback / link-local / reserved / multicast /
    unspecified addresses (IPv4 and IPv6). This covers 127.0.0.0/8, 10/8,
    172.16/12, 192.168/16, 169.254.0.0/16 (incl. the 169.254.169.254 cloud
    metadata endpoint), ::1, fe80::/10, fc00::/7, etc. A malformed address is
    treated as BLOCKED (fail closed).
    """
    try:
        ip = ipaddress.ip_address(str(ip_str).strip())
    except Exception:
        return True
    try:
        # IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) must be judged on the mapped
        # IPv4 address, otherwise loopback/private slips through as "global".
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        return bool(
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )
    except Exception:
        return True


def _resolve_ips(host: str) -> List[str]:
    """Resolve ``host`` to its unique IPs via getaddrinfo. NEVER raises.

    Module-level so tests can monkeypatch it deterministically (no real DNS in
    tests). Returns an empty list on any resolution failure or empty result.
    """
    try:
        if not host:
            return []
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return []
    ips: List[str] = []
    for info in infos:
        try:
            addr = info[4][0]
        except Exception:
            continue
        if addr and addr not in ips:
            ips.append(addr)
    return ips


def _url_is_safe(url: str) -> bool:
    """True only if ``url`` is safe to fetch server-side. NEVER raises.

    - scheme must be http/https
    - host must be present
    - host must resolve to at least one IP (empty resolution -> unsafe)
    - EVERY resolved IP must pass ``_ip_blocked`` (any private/loopback/...
      record -> the whole host is rejected; defeats a hostname that resolves to
      a private/link-local address, DNS-rebinding aware)
    On any error -> unsafe.
    """
    try:
        parsed = urlparse(str(url))
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname
        if not host:
            return False
        ips = _resolve_ips(host)
        if not ips:
            return False
        for ip in ips:
            if _ip_blocked(ip):
                return False
        return True
    except Exception:
        return False


class _TextExtractor(HTMLParser):
    """Collect human-readable text from HTML, dropping script/style/etc.

    Also records anchors (href + visible text) so the caller can heuristically
    locate an external company homepage link.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: List[str] = []
        self._skip_depth = 0
        # anchors: list of (href, anchor_text)
        self.anchors: List[tuple] = []
        self._cur_href: Optional[str] = None
        self._cur_anchor_text: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _DROP_TAGS:
            self._skip_depth += 1
            return
        if tag == "a":
            href = None
            for k, v in attrs:
                if k == "href":
                    href = v
                    break
            self._cur_href = href
            self._cur_anchor_text = []
        # Block-level tags act as whitespace boundaries so words don't fuse.
        if tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if tag == "a":
            text = " ".join(t.strip() for t in self._cur_anchor_text if t.strip())
            if self._cur_href:
                self.anchors.append((self._cur_href, text))
            self._cur_href = None
            self._cur_anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        self._chunks.append(data)
        if self._cur_href is not None:
            self._cur_anchor_text.append(data)

    def get_text(self) -> str:
        return _collapse_ws("".join(self._chunks))


def _collapse_ws(text: str) -> str:
    """Collapse all runs of whitespace to single spaces and trim. PURE."""
    return re.sub(r"\s+", " ", text or "").strip()


def html_to_text(html: str) -> str:
    """Strip HTML to collapsed plain text via stdlib html.parser. PURE.

    Drops script/style/etc. Returns "" on any parse error (never raises).
    """
    try:
        parser = _TextExtractor()
        parser.feed(html or "")
        parser.close()
        return parser.get_text()
    except Exception:
        return ""


def _looks_like_html(resp: "httpx.Response") -> bool:
    """True when the response content-type is HTML/text we can strip. PURE-ish."""
    ctype = (resp.headers.get("content-type") or "").lower()
    if not ctype:
        # No content-type header: accept and let the stripper decide.
        return True
    return any(t in ctype for t in _HTML_CONTENT_TYPES)


def _company_url_from_anchors(
    html: str, base_url: str, company: Optional[str]
) -> Optional[str]:
    """Best-effort: find ONE external company homepage link in the page. PURE.

    Heuristic: prefer an anchor whose visible text or href matches the company
    name; otherwise fall back to the first external (different-host) http(s)
    link. Returns an absolute URL or None. Never raises.
    """
    try:
        parser = _TextExtractor()
        parser.feed(html or "")
        parser.close()
    except Exception:
        return None

    base_host = (urlparse(base_url).hostname or "").lower()
    comp = (company or "").strip().lower()
    comp_tokens = [t for t in re.split(r"[^\wа-яё]+", comp, flags=re.I) if len(t) >= 3]

    external: List[str] = []
    for href, text in parser.anchors:
        if not href:
            continue
        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)
        if parsed.scheme not in ("http", "https"):
            continue
        host = (parsed.hostname or "").lower()
        if not host or host == base_host:
            continue
        # Skip obvious social / aggregator hosts that are never a company site.
        if any(
            bad in host
            for bad in (
                "t.me", "telegram", "facebook", "twitter", "x.com", "linkedin",
                "instagram", "youtube", "vk.com", "google.", "habr.com",
                "hh.ru", "github.com",
            )
        ):
            continue
        hay = f"{text} {abs_url}".lower()
        if comp and (comp in hay or any(tok in hay for tok in comp_tokens)):
            return abs_url
        external.append(abs_url)

    return external[0] if external else None


def _default_get(url: str, *, timeout: float, max_redirects: int) -> "httpx.Response":
    """Real synchronous httpx GET. Injectable for tests.

    ``follow_redirects=False``: redirects are handled MANUALLY in ``_fetch_one``
    so each redirect TARGET is re-resolved and SSRF-validated before we follow
    it (httpx's own auto-follow would resolve+connect without our IP guard).
    """
    client = httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        max_redirects=max_redirects,
        headers={"User-Agent": _USER_AGENT},
    )
    try:
        return client.get(url)
    finally:
        client.close()


# HTTP status codes that carry a redirect we may follow.
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def _fetch_one(
    url: str,
    get: Callable[..., "httpx.Response"],
    *,
    timeout: float,
    max_redirects: int,
    max_chars: int,
) -> Optional[Dict[str, str]]:
    """Fetch + strip ONE url, following redirects MANUALLY with SSRF re-checks.

    Returns {"url","text","html"} or None. NEVER raises.

    The CALLER validates the initial ``url`` with ``_url_is_safe`` before
    calling this. Here we follow up to ``max_redirects`` 3xx hops, and for EACH
    hop we re-resolve and re-validate the redirect TARGET with ``_url_is_safe``
    before fetching it. An unsafe redirect target aborts the fetch (-> None ->
    no page -> desk_fallback).
    """
    cur_url = str(url)
    budget = max(0, int(max_redirects))
    try:
        for _ in range(budget + 1):
            try:
                resp = get(cur_url, timeout=timeout, max_redirects=max_redirects)
            except Exception:
                return None

            status = int(getattr(resp, "status_code", 0) or 0)

            if status in _REDIRECT_STATUSES:
                if budget <= 0:
                    # No redirect budget left -> treat as non-success.
                    return None
                budget -= 1
                location = ""
                try:
                    location = (resp.headers.get("location") or "").strip()
                except Exception:
                    location = ""
                if not location:
                    return None
                target = urljoin(cur_url, location)
                # Re-validate the redirect TARGET's resolved IP before following.
                if not _url_is_safe(target):
                    return None
                cur_url = target
                continue

            if status < 200 or status >= 300:
                return None
            if not _looks_like_html(resp):
                return None
            html = getattr(resp, "text", "") or ""
            text = html_to_text(html)
            if len(text) < _MIN_USABLE_CHARS:
                # JS-only / empty / blocked-with-shell page -> not usable.
                return None
            if len(text) > max_chars:
                text = text[:max_chars]
            return {"url": str(cur_url), "text": text, "html": html}
        # Exhausted the hop loop without a final response.
        return None
    except Exception:
        return None


def fetch_research_context(
    source_link: Optional[str],
    company: Optional[str] = None,
    *,
    max_urls: int = 2,
    timeout: float = 8.0,
    max_redirects: int = 1,
    max_chars: int = 6000,
    get: Optional[Callable[..., "httpx.Response"]] = None,
) -> Dict[str, Any]:
    """Fetch real page text for the research leg. NEVER raises.

    Fetches ``source_link`` first. If that page exposes an obvious external
    company homepage link (best-effort heuristic against ``company``), fetches
    ONE more page. Hard cap of ``max_urls`` (default 2) URLs total.

    Returns ``{"pages": [{"url","text"}, ...], "urls": [...]}``. ``pages`` is
    empty whenever nothing usable was retrieved — the caller then falls back to
    desk research, matching today's behavior.
    """
    result: Dict[str, Any] = {"pages": [], "urls": []}
    try:
        if not source_link or not str(source_link).strip():
            return result
        url = str(source_link).strip()
        if urlparse(url).scheme not in ("http", "https"):
            return result
        # SSRF guard: validate the resolved IP(s) of source_link BEFORE fetching.
        # ``source_link`` comes from ingested Telegram posts (attacker-
        # influenceable). Unsafe -> empty pages -> desk_fallback.
        if not _url_is_safe(url):
            return result

        getter = get or _default_get
        cap = max(0, int(max_urls))
        if cap == 0:
            return result

        pages: List[Dict[str, str]] = []

        first = _fetch_one(
            url, getter, timeout=timeout, max_redirects=max_redirects, max_chars=max_chars
        )
        if first is not None:
            pages.append({"url": first["url"], "text": first["text"]})

            # Best-effort SECOND fetch: a company homepage discovered in the
            # first page. Only when we still have headroom under the cap.
            if len(pages) < cap:
                company_url = _company_url_from_anchors(
                    first.get("html", ""), url, company
                )
                # SSRF guard: validate the discovered company-anchor URL's
                # resolved IP(s) BEFORE the second fetch. Unsafe -> skip it.
                if (
                    company_url
                    and company_url != first["url"]
                    and _url_is_safe(company_url)
                ):
                    second = _fetch_one(
                        company_url,
                        getter,
                        timeout=timeout,
                        max_redirects=max_redirects,
                        max_chars=max_chars,
                    )
                    if second is not None:
                        pages.append({"url": second["url"], "text": second["text"]})

        result["pages"] = pages
        result["urls"] = [p["url"] for p in pages]
        return result
    except Exception:
        # Absolute belt-and-suspenders: this function must never raise.
        return {"pages": [], "urls": []}
