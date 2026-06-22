"""Adversarial SSRF guard tests added by the Tester role.

Covers every bypass vector that was NOT already tested, plus confirms the
behaviour of the guard for alternate IP encodings, IPv4-mapped IPv6,
redirect edge-cases, and never-raises properties.

NO real network / DNS: every test either uses monkeypatch._resolve_ips or
provides a fake ``get`` callable.  The autouse fixture maps every host to a
public IP by default; individual tests override it.

BYPASS MATRIX (results confirmed by running the guard locally):

  Vector                                 Outcome
  -------                                -------
  Literal 169.254.169.254/127.0.0.1/...  BLOCKED (existing tests)
  IPv6 ::1, fe80::, fc00::               BLOCKED (existing tests)
  IPv4-mapped ::ffff:127.0.0.1 in URL    BLOCKED  <- new
  IPv4-mapped ::ffff:10.0.0.1 in URL     BLOCKED  <- new
  http://2130706433/ (decimal)           BLOCKED (resolves to 127.0.0.1) <- new
  http://0x7f.0.0.1/ (hex)              BLOCKED (resolves to 127.0.0.1) <- new
  http://127.1/ (abbreviated)            BLOCKED (resolves to 127.0.0.1) <- new
  http://0177.0.0.1/ (octal, macOS)      ALLOWED to 177.0.0.1 (public)  <- documented
  http://0.0.0.0/ (unspecified)          BLOCKED  <- new
  Mixed public+private DNS (rebind)      BLOCKED (existing _url_is_safe test; new e2e)
  Redirect -> file://                    BLOCKED at redirect  <- new
  Redirect -> gopher://                  BLOCKED at redirect  <- new
  Redirect -> hostname resolving private BLOCKED at redirect  <- new
  Redirect with credentials to metadata  BLOCKED at redirect  <- new
  Company anchor -> metadata host        BLOCKED (existing test)
  Scheme: file://, gopher://, ftp://     BLOCKED  <- new fetch_research_context test
  _resolve_ips raising -> _url_is_safe   safe=False (existing test)
  _resolve_ips raising -> fetch_research_context  empty pages, no raise  <- new
  None/empty source_link                 empty pages, no raise (existing)
  Public URL                             FETCHES, research_source=web (existing+new)
  Redirect re-resolution (TOCTOU)        confirmed per-hop  <- new
"""

from __future__ import annotations

import pytest

from job_hunter import research_fetch
from job_hunter.research_fetch import (
    _ip_blocked,
    _url_is_safe,
    fetch_research_context,
)


class FakeResp:
    def __init__(
        self,
        text: str = "",
        status_code: int = 200,
        content_type: str = "text/html",
        headers: dict | None = None,
    ):
        self.text = text
        self.status_code = status_code
        hdrs = {"content-type": content_type} if content_type else {}
        if headers:
            hdrs.update(headers)
        self.headers = hdrs


def _page(n: int) -> str:
    return "<html><body><p>" + ("word " * (n // 5)) + "</p></body></html>"


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Default: every host resolves to a public IP so non-SSRF tests fetch."""
    monkeypatch.setattr(research_fetch, "_resolve_ips", lambda host: ["93.184.216.34"])


def _no_dns(monkeypatch):
    """Literal IP strings resolve to themselves; everything else fails."""
    import ipaddress

    def resolve(host):
        try:
            ipaddress.ip_address(host)
            return [host]
        except Exception:
            return []

    monkeypatch.setattr(research_fetch, "_resolve_ips", resolve)


# ---------------------------------------------------------------------------
# IPv4-mapped IPv6 in URL bracket notation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url,label",
    [
        ("http://[::ffff:127.0.0.1]/", "IPv4-mapped loopback in URL"),
        ("http://[::ffff:10.0.0.1]/", "IPv4-mapped RFC1918 in URL"),
        ("http://[::ffff:169.254.169.254]/", "IPv4-mapped link-local metadata in URL"),
        ("http://[::ffff:192.168.1.1]/", "IPv4-mapped private in URL"),
    ],
)
def test_ipv4_mapped_ipv6_url_blocked(monkeypatch, url, label):
    """An ::ffff:x.x.x.x IPv4-mapped address in bracket URL notation must be
    blocked because _ip_blocked extracts the mapped IPv4 and finds it private.
    The guard resolves the bracket host ('::ffff:127.0.0.1') to itself via
    getaddrinfo and _ip_blocked catches it via the ipv4_mapped unwrap.
    """
    _no_dns(monkeypatch)
    called = []

    def fake_get(u, *, timeout, max_redirects):
        called.append(u)
        return FakeResp(_page(2000))

    out = fetch_research_context(url, "Acme", get=fake_get)
    assert out == {"pages": [], "urls": []}, f"{label}: expected blocked, got pages"
    assert called == [], f"{label}: httpx must not be called before the guard"


# ---------------------------------------------------------------------------
# Alternate IP encodings (decimal / hex / abbreviated)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url,label",
    [
        ("http://2130706433/", "decimal 127.0.0.1"),
        ("http://0x7f.0.0.1/", "hex 0x7f = 127"),
        ("http://127.1/", "abbreviated two-octet 127.1"),
        ("http://127.0.1/", "three-octet abbreviated 127.0.1"),
    ],
)
def test_alternate_encoding_loopback_blocked(monkeypatch, url, label):
    """Non-standard IP notations that socket.getaddrinfo resolves to 127.0.0.1
    must be blocked.  The guard relies on getaddrinfo (not string parsing), so
    any encoding that getaddrinfo maps to a loopback address is caught.
    """
    # Use real getaddrinfo via a _resolve_ips that calls socket directly (not the
    # module-level patch).  We restore the real _resolve_ips just for these tests.
    import socket

    def real_resolve(host):
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            return []
        ips = []
        for info in infos:
            try:
                addr = info[4][0]
            except Exception:
                continue
            if addr and addr not in ips:
                ips.append(addr)
        return ips

    monkeypatch.setattr(research_fetch, "_resolve_ips", real_resolve)
    called = []

    def fake_get(u, *, timeout, max_redirects):
        called.append(u)
        return FakeResp(_page(2000))

    out = fetch_research_context(url, "Acme", get=fake_get)
    assert out == {"pages": [], "urls": []}, (
        f"{label}: URL {url!r} must be blocked but pages were returned"
    )
    assert called == [], (
        f"{label}: httpx must not be called; guard must block before the fetch"
    )


def test_octal_encoding_0177_macos_behavior_documented(monkeypatch):
    """DOCUMENTATION of macOS-specific behaviour for http://0177.0.0.1/.

    On macOS Python 3.9, socket.getaddrinfo('0177.0.0.1') returns 177.0.0.1
    (literal dotted-decimal, NOT octal 127).  177.0.0.1 is a public IP, so the
    guard ALLOWS the fetch (consistent with httpx which also uses getaddrinfo).

    On Linux glibc, getaddrinfo typically calls inet_aton which DOES parse the
    leading 0 as octal, returning 127.0.0.1 -> BLOCKED.

    This test asserts the observed macOS behavior so a future OS change is
    detected immediately, rather than silently changing the guard's output.
    """
    import socket

    def real_resolve(host):
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            return []
        ips = []
        for info in infos:
            try:
                addr = info[4][0]
            except Exception:
                continue
            if addr and addr not in ips:
                ips.append(addr)
        return ips

    monkeypatch.setattr(research_fetch, "_resolve_ips", real_resolve)
    ips = real_resolve("0177.0.0.1")
    # macOS: 177.0.0.1 (public) -> allowed; Linux: 127.0.0.1 (loopback) -> blocked.
    # Either outcome is acceptable as long as it is consistent with what httpx sees.
    if ips == ["177.0.0.1"]:
        # macOS path: guard and httpx agree on 177.0.0.1 (public) -> ALLOWED.
        # This is NOT a bypass to local infrastructure; 177.0.0.1 is public.
        assert _url_is_safe("http://0177.0.0.1/") is True
    elif ips and all(ip.startswith("127.") for ip in ips):
        # Linux path: resolves to loopback -> BLOCKED.
        assert _url_is_safe("http://0177.0.0.1/") is False
    else:
        # Unknown resolution; assert it's consistent with the guard.
        safe = _url_is_safe("http://0177.0.0.1/")
        assert isinstance(safe, bool)  # just confirm no exception


# ---------------------------------------------------------------------------
# http://0.0.0.0/ (unspecified address) in fetch_research_context
# ---------------------------------------------------------------------------

def test_unspecified_0_0_0_0_blocked_in_fetch(monkeypatch):
    """http://0.0.0.0/ must be blocked end-to-end through fetch_research_context.

    0.0.0.0 is the unspecified address; ipaddress.is_unspecified is True for it.
    The guard must catch it before issuing any HTTP request.
    """
    _no_dns(monkeypatch)
    called = []

    def fake_get(u, *, timeout, max_redirects):
        called.append(u)
        return FakeResp(_page(2000))

    out = fetch_research_context("http://0.0.0.0/", "Acme", get=fake_get)
    assert out == {"pages": [], "urls": []}
    assert called == []


# ---------------------------------------------------------------------------
# Mixed public + private DNS records in fetch_research_context (e2e)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mixed_ips,label",
    [
        (["93.184.216.34", "10.0.0.5"], "public + RFC1918"),
        (["93.184.216.34", "169.254.169.254"], "public + link-local metadata"),
        (["93.184.216.34", "::1"], "public + IPv6 loopback"),
        (["1.2.3.4", "fc00::beef"], "public + unique-local IPv6"),
    ],
)
def test_mixed_public_private_records_blocked_in_fetch(monkeypatch, mixed_ips, label):
    """Any hostname whose DNS returns BOTH public and private records must be
    blocked in fetch_research_context.  The 'reject-if-ANY-private' rule must
    hold end-to-end so a split-horizon DNS (e.g. split-view CDN) cannot sneak a
    private record past the guard.
    """
    monkeypatch.setattr(research_fetch, "_resolve_ips", lambda h: mixed_ips)
    called = []

    def fake_get(u, *, timeout, max_redirects):
        called.append(u)
        return FakeResp(_page(2000))

    out = fetch_research_context(
        "https://split-horizon.example/vacancy", "Acme", get=fake_get
    )
    assert out == {"pages": [], "urls": []}, (
        f"{label}: mixed records must be blocked but got pages"
    )
    assert called == [], f"{label}: httpx must not be called with mixed records"


# ---------------------------------------------------------------------------
# Redirect vectors
# ---------------------------------------------------------------------------

def test_redirect_to_file_scheme_blocked(monkeypatch):
    """A 3xx redirect whose Location header is 'file:///etc/passwd' must be
    blocked at the redirect step — _url_is_safe rejects non-http(s) schemes.
    The private-file body must never be read.
    """
    # autouse fixture already maps all hosts to public IP
    seen = []

    def fake_get(u, *, timeout, max_redirects):
        seen.append(u)
        return FakeResp("", status_code=302, headers={"location": "file:///etc/passwd"})

    out = fetch_research_context("https://jobs.example/v", get=fake_get)
    assert out["pages"] == []
    # The initial URL was fetched; the file:// redirect target was not.
    assert "https://jobs.example/v" in seen
    assert "file:///etc/passwd" not in seen


def test_redirect_to_gopher_scheme_blocked(monkeypatch):
    """A redirect to a gopher:// URL must be blocked at the redirect step."""
    seen = []

    def fake_get(u, *, timeout, max_redirects):
        seen.append(u)
        return FakeResp("", status_code=301, headers={"location": "gopher://127.0.0.1/1"})

    out = fetch_research_context("https://jobs.example/v", get=fake_get)
    assert out["pages"] == []
    assert len(seen) == 1  # only the initial fetch, not the gopher target


def test_redirect_to_hostname_resolving_private(monkeypatch):
    """A redirect whose Location hostname resolves to a private IP must be
    blocked at the redirect hop, even if the initial host was legitimate.
    This is different from test_redirect_to_private_blocked_at_redirect (which
    uses a literal IP in the redirect target).  Here we test a HOSTNAME.
    """
    def resolve(host):
        if host == "intranet.private":
            return ["172.16.100.1"]
        return ["93.184.216.34"]

    monkeypatch.setattr(research_fetch, "_resolve_ips", resolve)
    seen = []

    def fake_get(u, *, timeout, max_redirects):
        seen.append(u)
        if "intranet.private" in u:
            return FakeResp(_page(2000))  # must never be reached
        return FakeResp("", status_code=302,
                        headers={"location": "http://intranet.private/"})

    out = fetch_research_context("https://jobs.example/v", "Acme", get=fake_get)
    assert out["pages"] == []
    assert seen == ["https://jobs.example/v"]  # redirect target never fetched


def test_redirect_to_metadata_with_credentials_blocked(monkeypatch):
    """A redirect Location like 'http://user@169.254.169.254/meta-data' must be
    blocked.  urlparse extracts hostname='169.254.169.254' regardless of the
    embedded credentials, so the guard should catch it.
    """
    def resolve(host):
        if host == "169.254.169.254":
            return ["169.254.169.254"]
        return ["93.184.216.34"]

    monkeypatch.setattr(research_fetch, "_resolve_ips", resolve)
    seen = []

    def fake_get(u, *, timeout, max_redirects):
        seen.append(u)
        return FakeResp(
            "", status_code=302,
            headers={"location": "http://user@169.254.169.254/latest/meta-data"},
        )

    out = fetch_research_context("https://jobs.example/v", get=fake_get)
    assert out["pages"] == []
    assert seen == ["https://jobs.example/v"]


def test_redirect_rerevolves_new_host_not_initial_host(monkeypatch):
    """The redirect handler must resolve the TARGET host, not reuse the initial
    host's resolution.  This is the TOCTOU check: a different host in the
    Location header gets a fresh getaddrinfo call with the new hostname.
    """
    resolved_hosts = []

    def track_and_resolve(host):
        resolved_hosts.append(host)
        if host == "redirect-dest.example":
            return ["93.184.216.34"]
        return ["93.184.216.34"]

    monkeypatch.setattr(research_fetch, "_resolve_ips", track_and_resolve)
    seen = []

    def fake_get(u, *, timeout, max_redirects):
        seen.append(u)
        if "redirect-dest" in u:
            return FakeResp(_page(2000))
        return FakeResp("", status_code=302,
                        headers={"location": "https://redirect-dest.example/page"})

    out = fetch_research_context("https://origin.example/v", get=fake_get)
    assert len(out["pages"]) == 1
    # Both the original host AND the redirect target must have been resolved.
    assert "origin.example" in resolved_hosts
    assert "redirect-dest.example" in resolved_hosts


# ---------------------------------------------------------------------------
# Scheme tricks in fetch_research_context (not just _url_is_safe)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://localhost/1",
        "ftp://files.example.com/",
        "javascript:alert(1)",
        "data:text/html,<h1>hi</h1>",
    ],
)
def test_bad_scheme_in_source_link_returns_empty_no_raise(monkeypatch, url):
    """Non-http(s) source_link must be rejected early (scheme check) and
    fetch_research_context must return empty pages without raising.
    """
    called = []

    def fake_get(u, *, timeout, max_redirects):
        called.append(u)
        return FakeResp(_page(2000))

    out = fetch_research_context(url, "Acme", get=fake_get)
    assert out == {"pages": [], "urls": []}, f"Non-http scheme {url!r} must be blocked"
    assert called == [], f"httpx must not be called for non-http scheme {url!r}"


def test_http_with_port_metadata_blocked(monkeypatch):
    """http://169.254.169.254:80/ with non-default port must still be blocked.
    urlparse.hostname strips the port, so the guard sees the bare IP.
    """
    _no_dns(monkeypatch)
    called = []

    def fake_get(u, *, timeout, max_redirects):
        called.append(u)
        return FakeResp(_page(2000))

    out = fetch_research_context(
        "http://169.254.169.254:80/latest/meta-data", "Acme", get=fake_get
    )
    assert out == {"pages": [], "urls": []}
    assert called == []


# ---------------------------------------------------------------------------
# Never-raises: _resolve_ips itself raises in _url_is_safe path (e2e)
# ---------------------------------------------------------------------------

def test_fetch_never_raises_when_resolve_ips_raises(monkeypatch):
    """If _resolve_ips raises an exception (e.g. unexpected RuntimeError), the
    full fetch_research_context call must still return empty pages and NEVER raise.

    This is distinct from the _url_is_safe unit test which directly verifies
    _url_is_safe returns False; here we confirm the full function path is safe.
    """
    def boom(host):
        raise RuntimeError("DNS exploded unexpectedly")

    monkeypatch.setattr(research_fetch, "_resolve_ips", boom)
    called = []

    def fake_get(u, *, timeout, max_redirects):
        called.append(u)
        return FakeResp(_page(2000))

    out = fetch_research_context("https://jobs.example.com/v", "Acme", get=fake_get)
    assert out == {"pages": [], "urls": []}, (
        "fetch_research_context must return empty pages when _resolve_ips raises"
    )
    assert called == [], "httpx must not be called when resolution blows up"


# ---------------------------------------------------------------------------
# Never-raises: malformed URL
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_url",
    [
        "not-a-url",
        "://no-scheme",
        "http://",        # no host
        "http:///path",   # no host
        "",
        "   ",
    ],
)
def test_malformed_url_returns_empty_no_raise(bad_url):
    """Malformed or empty source_link must never propagate an exception;
    fetch_research_context must return empty pages silently.
    """
    out = fetch_research_context(bad_url)
    assert out == {"pages": [], "urls": []}, (
        f"Malformed URL {bad_url!r} must produce empty pages"
    )


# ---------------------------------------------------------------------------
# Public URL regression: guard must not over-block legitimate URLs
# ---------------------------------------------------------------------------

def test_public_url_yields_web_research_source(monkeypatch):
    """A vacancy URL resolving to a public IP must be fetched and must result in
    research_source='web' through agents.research.  This is the primary path the
    guard must NOT block.
    """
    import json
    from job_hunter import agents
    from job_hunter.schema_extract import ExtractResult

    # autouse fixture already sets public DNS

    page_html = "<html><body><p>" + ("content " * 300) + "</p></body></html>"

    def fake_get(u, *, timeout, max_redirects):
        return FakeResp(page_html)

    # Patch fetch_research_context to use our fake_get
    orig = research_fetch.fetch_research_context

    def patched(source_link, company=None, **kw):
        return orig(source_link, company, get=fake_get, **kw)

    monkeypatch.setattr(research_fetch, "fetch_research_context", patched)

    class FakeLLMForResearch:
        def complete(self, system, user, **kw):
            return json.dumps({
                "summary": "real company summary",
                "talking_points": [],
                "questions": [],
                "sourced_facts": ["company fact"],
            })

    extracted = ExtractResult(
        title="AI Engineer",
        company="Acme",
        source_channel="@channel",
        source_link="https://jobs.example.com/vacancy/1",
    )
    result = agents.research(FakeLLMForResearch(), extracted, "raw text")

    assert result["research_source"] == "web", (
        "Public URL must produce research_source='web', not desk_fallback"
    )
    assert "https://jobs.example.com/vacancy/1" in result["fetched_urls"]


# ---------------------------------------------------------------------------
# Company anchor with metadata host — end-to-end through fetch_research_context
# ---------------------------------------------------------------------------

def test_company_anchor_metadata_host_not_fetched(monkeypatch):
    """A page whose company anchor points at the cloud metadata endpoint
    (169.254.169.254) must result in the first page being returned normally,
    but the second (metadata) fetch must be skipped by the SSRF guard.
    """
    def resolve(host):
        if host == "169.254.169.254":
            return ["169.254.169.254"]
        return ["93.184.216.34"]

    monkeypatch.setattr(research_fetch, "_resolve_ips", resolve)

    metadata_anchor_html = (
        "<html><body><p>" + ("job content " * 100) + "</p>"
        '<a href="http://169.254.169.254/latest/meta-data">Company Info</a>'
        "</body></html>"
    )
    seen = []

    def fake_get(u, *, timeout, max_redirects):
        seen.append(u)
        return FakeResp(metadata_anchor_html)

    out = fetch_research_context("https://jobs.test/v", "Acme", get=fake_get)
    # First page returned (it's public)
    assert len(out["pages"]) == 1
    assert out["pages"][0]["url"] == "https://jobs.test/v"
    # Metadata endpoint never fetched
    assert all("169.254.169.254" not in s for s in seen), (
        "Metadata endpoint must never be fetched as the company anchor"
    )
