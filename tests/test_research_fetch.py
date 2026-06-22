"""Tests for the direct-URL research fetcher (Phase 1).

NO real network: every test injects a fake ``get`` (or monkeypatches the
default httpx client). Asserts the NEVER-RAISES contract, the truncation cap,
the URL limit, and the company-page discovery.
"""

from __future__ import annotations

import httpx
import pytest

from job_hunter import research_fetch
from job_hunter.research_fetch import (
    fetch_research_context,
    html_to_text,
    _ip_blocked,
    _url_is_safe,
)


class FakeResp:
    def __init__(self, text="", status_code=200, content_type="text/html", headers=None):
        self.text = text
        self.status_code = status_code
        hdrs = {"content-type": content_type} if content_type else {}
        if headers:
            hdrs.update(headers)
        self.headers = hdrs


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Default: every host resolves to a public IP so non-SSRF tests fetch.

    SSRF tests override _resolve_ips themselves. No real DNS is ever performed.
    """
    monkeypatch.setattr(research_fetch, "_resolve_ips", lambda host: ["93.184.216.34"])


def _page(body_chars: int) -> str:
    return "<html><body><p>" + ("слово " * (body_chars // 6)) + "</p></body></html>"


# --- html_to_text (PURE) ----------------------------------------------------

def test_html_to_text_drops_script_style_and_collapses_ws():
    html = (
        "<html><head><style>.x{color:red}</style></head>"
        "<body><script>var a=1;</script>"
        "<h1>Acme   Corp</h1>\n\n<p>We build\tLLM   tools.</p></body></html>"
    )
    text = html_to_text(html)
    assert "color:red" not in text
    assert "var a=1" not in text
    assert "Acme Corp" in text
    assert "We build LLM tools." in text
    # whitespace collapsed
    assert "  " not in text


def test_html_to_text_never_raises_on_garbage():
    assert html_to_text("<<<>>> <unclosed <b") == html_to_text("<<<>>> <unclosed <b")
    assert isinstance(html_to_text(None), str)


# --- success path -----------------------------------------------------------

def test_success_returns_page_text_and_url():
    calls = []

    def fake_get(url, *, timeout, max_redirects):
        calls.append((url, timeout, max_redirects))
        return FakeResp(_page(2000))

    out = fetch_research_context("https://jobs.example.com/v/1", "Acme", get=fake_get)
    assert len(out["pages"]) == 1
    assert out["pages"][0]["url"] == "https://jobs.example.com/v/1"
    assert "слово" in out["pages"][0]["text"]
    assert out["urls"] == ["https://jobs.example.com/v/1"]
    # timeout + redirect cap passed through
    assert calls[0][1] == 8.0
    assert calls[0][2] == 1


# --- failure modes all route to empty, never raise --------------------------

def test_timeout_returns_empty_no_raise():
    def fake_get(url, *, timeout, max_redirects):
        raise httpx.TimeoutException("boom")

    out = fetch_research_context("https://x.test/v", "Acme", get=fake_get)
    assert out == {"pages": [], "urls": []}


def test_connection_error_returns_empty():
    def fake_get(url, *, timeout, max_redirects):
        raise httpx.ConnectError("refused")

    out = fetch_research_context("https://x.test/v", get=fake_get)
    assert out["pages"] == []


def test_404_returns_empty():
    def fake_get(url, *, timeout, max_redirects):
        return FakeResp("<html><body>nope</body></html>", status_code=404)

    out = fetch_research_context("https://x.test/v", get=fake_get)
    assert out["pages"] == []


def test_500_returns_empty():
    def fake_get(url, *, timeout, max_redirects):
        return FakeResp(_page(2000), status_code=503)

    assert fetch_research_context("https://x.test/v", get=fake_get)["pages"] == []


def test_non_html_content_type_discarded():
    def fake_get(url, *, timeout, max_redirects):
        return FakeResp("%PDF-1.4 ...lots of bytes...", content_type="application/pdf")

    assert fetch_research_context("https://x.test/f.pdf", get=fake_get)["pages"] == []


def test_empty_after_strip_discarded():
    def fake_get(url, *, timeout, max_redirects):
        return FakeResp("<html><body><p></p></body></html>")

    assert fetch_research_context("https://x.test/v", get=fake_get)["pages"] == []


def test_js_only_short_page_discarded():
    # Loads fine but stripped text < 200-char threshold -> not usable.
    def fake_get(url, *, timeout, max_redirects):
        return FakeResp("<html><body><div id='root'></div><p>Loading</p></body></html>")

    assert fetch_research_context("https://x.test/v", get=fake_get)["pages"] == []


def test_no_source_link_returns_empty():
    assert fetch_research_context(None, "Acme")["pages"] == []
    assert fetch_research_context("", "Acme")["pages"] == []


def test_non_http_scheme_returns_empty():
    assert fetch_research_context("ftp://x.test/v", get=lambda *a, **k: FakeResp(_page(2000)))["pages"] == []


# --- truncation -------------------------------------------------------------

def test_truncation_caps_text_before_llm():
    huge = "<html><body><p>" + ("a" * 50000) + "</p></body></html>"

    def fake_get(url, *, timeout, max_redirects):
        return FakeResp(huge)

    out = fetch_research_context("https://x.test/v", get=fake_get, max_chars=6000)
    assert len(out["pages"]) == 1
    assert len(out["pages"][0]["text"]) <= 6000


# --- URL limits + company-page discovery ------------------------------------

def test_company_page_fetched_when_link_found_max_two():
    vacancy_html = (
        "<html><body><p>" + ("работа " * 60) + "</p>"
        "<a href='https://acme.io'>Acme official site</a></body></html>"
    )
    company_html = "<html><body><p>" + ("acme " * 100) + "</p></body></html>"
    seen = []

    def fake_get(url, *, timeout, max_redirects):
        seen.append(url)
        if "acme.io" in url:
            return FakeResp(company_html)
        return FakeResp(vacancy_html)

    out = fetch_research_context("https://jobs.test/v/1", "Acme", get=fake_get)
    assert len(out["pages"]) == 2
    assert out["urls"] == ["https://jobs.test/v/1", "https://acme.io"]
    assert len(seen) == 2  # never exceeds 2 total


def test_no_second_fetch_when_no_company_link():
    vacancy_html = "<html><body><p>" + ("работа " * 60) + "</p></body></html>"
    seen = []

    def fake_get(url, *, timeout, max_redirects):
        seen.append(url)
        return FakeResp(vacancy_html)

    out = fetch_research_context("https://jobs.test/v/1", "Acme", get=fake_get)
    assert len(out["pages"]) == 1
    assert len(seen) == 1


def test_max_urls_one_prevents_company_fetch():
    vacancy_html = (
        "<html><body><p>" + ("работа " * 60) + "</p>"
        "<a href='https://acme.io'>Acme</a></body></html>"
    )
    seen = []

    def fake_get(url, *, timeout, max_redirects):
        seen.append(url)
        return FakeResp(vacancy_html)

    out = fetch_research_context("https://jobs.test/v/1", "Acme", get=fake_get, max_urls=1)
    assert len(out["pages"]) == 1
    assert len(seen) == 1


def test_social_links_not_treated_as_company():
    vacancy_html = (
        "<html><body><p>" + ("работа " * 60) + "</p>"
        "<a href='https://t.me/somechan'>tg</a>"
        "<a href='https://github.com/x'>gh</a></body></html>"
    )
    seen = []

    def fake_get(url, *, timeout, max_redirects):
        seen.append(url)
        return FakeResp(vacancy_html)

    out = fetch_research_context("https://jobs.test/v/1", "Acme", get=fake_get)
    assert len(out["pages"]) == 1  # social/aggregator links skipped
    assert len(seen) == 1


def test_default_get_uses_httpx_with_caps(monkeypatch):
    """The default getter wires httpx.Client with timeout + redirect cap + UA."""
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def get(self, url):
            captured["url"] = url
            return FakeResp(_page(2000))

        def close(self):
            pass

    monkeypatch.setattr(research_fetch.httpx, "Client", FakeClient)
    out = fetch_research_context("https://x.test/v")
    assert out["pages"]
    assert captured["timeout"] == 8.0
    assert captured["max_redirects"] == 1
    # Redirects are handled manually (with per-hop SSRF revalidation), so the
    # client itself must NOT auto-follow.
    assert captured["follow_redirects"] is False
    assert "User-Agent" in captured["headers"]


# --- SSRF GUARD =============================================================

# --- _ip_blocked (PURE) -----------------------------------------------------

@pytest.mark.parametrize(
    "ip",
    [
        "169.254.169.254",  # cloud metadata (link-local)
        "127.0.0.1",        # loopback
        "10.0.0.1",         # RFC1918
        "192.168.1.1",      # RFC1918
        "172.16.0.1",       # RFC1918
        "0.0.0.0",          # unspecified
        "::1",              # IPv6 loopback
        "fe80::1",          # IPv6 link-local
        "fc00::1",          # IPv6 unique-local (is_private)
        "::ffff:127.0.0.1", # IPv4-mapped loopback
        "ff02::1",          # IPv6 multicast
        "not-an-ip",        # malformed -> blocked (fail closed)
        "",                 # empty -> blocked
    ],
)
def test_ip_blocked_blocks_dangerous(ip):
    assert _ip_blocked(ip) is True


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"])
def test_ip_blocked_allows_public(ip):
    assert _ip_blocked(ip) is False


# --- _url_is_safe ------------------------------------------------------------

def test_url_is_safe_rejects_non_http_scheme(monkeypatch):
    monkeypatch.setattr(research_fetch, "_resolve_ips", lambda h: ["93.184.216.34"])
    assert _url_is_safe("ftp://example.com/") is False
    assert _url_is_safe("file:///etc/passwd") is False


def test_url_is_safe_rejects_empty_resolution(monkeypatch):
    monkeypatch.setattr(research_fetch, "_resolve_ips", lambda h: [])
    assert _url_is_safe("https://nxdomain.test/") is False


def test_url_is_safe_rejects_if_any_record_private(monkeypatch):
    # One public + one private record -> reject the WHOLE host.
    monkeypatch.setattr(
        research_fetch, "_resolve_ips", lambda h: ["93.184.216.34", "10.0.0.5"]
    )
    assert _url_is_safe("https://rebind.test/") is False


def test_url_is_safe_allows_all_public(monkeypatch):
    monkeypatch.setattr(research_fetch, "_resolve_ips", lambda h: ["93.184.216.34"])
    assert _url_is_safe("https://example.com/") is True


def test_url_is_safe_never_raises(monkeypatch):
    def boom(h):
        raise RuntimeError("dns blew up")

    monkeypatch.setattr(research_fetch, "_resolve_ips", boom)
    assert _url_is_safe("https://example.com/") is False


# --- literal dangerous IPs in source_link -> blocked, pages empty -----------

def _no_dns(monkeypatch):
    """Real resolution: literal IP hosts resolve to themselves; names -> []."""
    def resolve(host):
        try:
            import ipaddress
            ipaddress.ip_address(host)
            return [host]
        except Exception:
            return []
    monkeypatch.setattr(research_fetch, "_resolve_ips", resolve)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://[::1]/",
    ],
)
def test_literal_dangerous_ip_blocked(monkeypatch, url):
    _no_dns(monkeypatch)
    called = []

    def fake_get(u, *, timeout, max_redirects):
        called.append(u)
        return FakeResp(_page(2000))

    out = fetch_research_context(url, "Acme", get=fake_get)
    assert out == {"pages": [], "urls": []}
    assert called == []  # never even attempted the fetch


def test_localhost_name_blocked(monkeypatch):
    monkeypatch.setattr(research_fetch, "_resolve_ips", lambda h: ["127.0.0.1"])
    called = []

    def fake_get(u, *, timeout, max_redirects):
        called.append(u)
        return FakeResp(_page(2000))

    out = fetch_research_context("http://localhost/", "Acme", get=fake_get)
    assert out["pages"] == []
    assert called == []


def test_hostname_resolving_to_private_blocked(monkeypatch):
    # Proves we block by RESOLVED IP, not by string — innocent-looking name.
    monkeypatch.setattr(research_fetch, "_resolve_ips", lambda h: ["10.0.0.5"])
    called = []

    def fake_get(u, *, timeout, max_redirects):
        called.append(u)
        return FakeResp(_page(2000))

    out = fetch_research_context("http://totally-legit.example/", "Acme", get=fake_get)
    assert out["pages"] == []
    assert called == []


@pytest.mark.parametrize("ipv6", ["fe80::1", "fc00::dead"])
def test_hostname_resolving_to_ipv6_private_blocked(monkeypatch, ipv6):
    monkeypatch.setattr(research_fetch, "_resolve_ips", lambda h: [ipv6])
    out = fetch_research_context("http://corp-intranet.example/", "Acme",
                                 get=lambda *a, **k: FakeResp(_page(2000)))
    assert out["pages"] == []


def test_public_url_still_fetched(monkeypatch):
    # The autouse _public_dns fixture already maps everything to a public IP.
    def fake_get(u, *, timeout, max_redirects):
        return FakeResp(_page(2000))

    out = fetch_research_context("https://jobs.example.com/v/1", "Acme", get=fake_get)
    assert len(out["pages"]) == 1
    assert out["pages"][0]["url"] == "https://jobs.example.com/v/1"


# --- redirect revalidation --------------------------------------------------

def test_redirect_to_private_blocked_at_redirect(monkeypatch):
    # Public host resolves fine; the 302 points at a private IP -> abort fetch.
    def resolve(host):
        if host == "10.0.0.1":
            return ["10.0.0.1"]
        return ["93.184.216.34"]

    monkeypatch.setattr(research_fetch, "_resolve_ips", resolve)
    seen = []

    def fake_get(u, *, timeout, max_redirects):
        seen.append(u)
        if "10.0.0.1" in u:
            # Should NEVER be reached — guard must abort before this fetch.
            return FakeResp(_page(2000))
        return FakeResp("", status_code=302, headers={"location": "http://10.0.0.1/"})

    out = fetch_research_context("https://public.example/v", "Acme", get=fake_get)
    assert out["pages"] == []
    # First (public) URL was fetched; the private redirect target was NOT.
    assert seen == ["https://public.example/v"]


def test_redirect_to_public_followed(monkeypatch):
    def resolve(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(research_fetch, "_resolve_ips", resolve)
    seen = []

    def fake_get(u, *, timeout, max_redirects):
        seen.append(u)
        if u == "https://public.example/final":
            return FakeResp(_page(2000))
        return FakeResp("", status_code=302,
                        headers={"location": "https://public.example/final"})

    out = fetch_research_context("https://public.example/v", "Acme", get=fake_get)
    assert len(out["pages"]) == 1
    assert out["pages"][0]["url"] == "https://public.example/final"
    assert seen == ["https://public.example/v", "https://public.example/final"]


def test_redirect_beyond_budget_not_followed(monkeypatch):
    monkeypatch.setattr(research_fetch, "_resolve_ips", lambda h: ["93.184.216.34"])
    seen = []

    def fake_get(u, *, timeout, max_redirects):
        seen.append(u)
        # Always redirect -> with budget 1 we follow once then give up.
        return FakeResp("", status_code=302, headers={"location": "https://public.example/next"})

    out = fetch_research_context("https://public.example/v", "Acme", get=fake_get,
                                 max_redirects=1)
    assert out["pages"] == []
    # initial + one followed hop, then budget exhausted.
    assert len(seen) == 2


# --- company anchor pointing at a private IP --------------------------------

def test_company_anchor_to_private_skipped_first_still_returned(monkeypatch):
    def resolve(host):
        if host == "intranet.local":
            return ["192.168.0.10"]
        return ["93.184.216.34"]

    monkeypatch.setattr(research_fetch, "_resolve_ips", resolve)

    vacancy_html = (
        "<html><body><p>" + ("работа " * 60) + "</p>"
        "<a href='http://intranet.local/'>Acme official site</a></body></html>"
    )
    seen = []

    def fake_get(u, *, timeout, max_redirects):
        seen.append(u)
        return FakeResp(vacancy_html)

    out = fetch_research_context("https://jobs.test/v/1", "Acme", get=fake_get)
    assert len(out["pages"]) == 1  # first public page returned
    assert out["pages"][0]["url"] == "https://jobs.test/v/1"
    assert seen == ["https://jobs.test/v/1"]  # private anchor never fetched


# --- integration through agents.research: blocked -> desk_fallback, no raise -

def test_agents_research_desk_fallback_on_blocked(monkeypatch):
    from job_hunter import agents
    from job_hunter.extract import ExtractResult

    # source_link resolves to cloud-metadata IP -> blocked -> empty pages.
    monkeypatch.setattr(research_fetch, "_resolve_ips", lambda h: ["169.254.169.254"])

    class FakeLLMClient:
        pass

    def fake_llm_research(client, extracted, raw_text, *, model, fetched_context):
        # With a blocked fetch, fetched_context must be None (desk-only).
        assert fetched_context is None
        return {"summary": "desk", "talking_points": [], "questions": [],
                "sourced_facts": [{"fact": "should be cleared"}]}

    monkeypatch.setattr(agents.llm, "llm_research", fake_llm_research)

    extracted = ExtractResult(
        title="Engineer",
        source_channel="@somechan",
        company="Acme",
        source_link="http://169.254.169.254/latest/meta-data/",
    )

    data = agents.research(FakeLLMClient(), extracted, "raw post text")
    assert data["research_source"] == "desk_fallback"
    assert data["fetched_urls"] == []
    assert data["sourced_facts"] == []  # honesty: no grounding -> no facts
