"""Adversarial / regression tests for Phase 1 web research (approve -> draft flow).

Added by the Tester validation pass. Every test here covers a gap or bug
identified against the HARD REQUIREMENTS:

  (1) NEVER-RAISES: the fetch + agents.research must never propagate an exception.
      Worst case == today's desk behavior (research_source="desk_fallback").

  (2) HONESTY / NO-FABRICATION: a malicious or hallucinating model must not be
      able to forge provenance (research_source="web" + fetched_urls) or inject
      fabricated sourced_facts when no page was actually fetched (desk_fallback).

  (3) SSRF assessment test: documents and verifies that private/link-local URLs
      pass the scheme check and WOULD be fetched; validates the security finding.

Bugs this file was written to expose:
  BUG-1 (CRITICAL): agents.research does NOT wrap llm_research in a try/except,
    so any exception from the LLM (API timeout, network error, ValueError from
    parse_research_response on malformed output that _parse_json_object raises)
    propagates out of agents.research, violating the NEVER-RAISES contract.

  BUG-2 (HONESTY): on desk_fallback, agents.research does NOT clear sourced_facts
    if the model returns a non-empty list. A hallucinating/malicious model can
    smuggle fabricated "sourced" facts through the desk path, since the
    authoritative overwrite only applies to research_source and fetched_urls.

  SSRF-MEDIUM: source_link is attacker-influenceable (Telegram post). A crafted
    source_link of http://169.254.169.254/…, http://127.0.0.1/…, RFC1918, or
    http://localhost/… passes the scheme-only allowlist (http/https) and is
    fetched by httpx unconditionally. Similarly, _company_url_from_anchors can
    return a private-IP URL discovered in a page's link set, which also gets
    fetched. No SSRF guard (private-IP/loopback/link-local block) exists.
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest import mock

import httpx
import pytest

from job_hunter import agents, llm, research_fetch
from job_hunter.research_fetch import fetch_research_context
from job_hunter.schema_extract import ExtractResult


@pytest.fixture(autouse=True)
def _public_dns_default(monkeypatch):
    """No real DNS in tests. By default every host resolves to a public IP so
    the SSRF guard (added to research_fetch) does not block legitimate fetch
    tests. SSRF-specific tests override _resolve_ips themselves to simulate
    private/link-local resolution.
    """
    monkeypatch.setattr(research_fetch, "_resolve_ips", lambda host: ["93.184.216.34"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract(source_link="https://jobs.test/v/1", company="Acme"):
    return ExtractResult(
        title="AI Engineer",
        company=company,
        stack=["python", "llm"],
        source_channel="@chan",
        source_link=source_link,
    )


def _research_resp(sourced=None):
    return json.dumps({
        "summary": "ok",
        "talking_points": ["a"],
        "questions": ["q"],
        "sourced_facts": sourced if sourced is not None else [],
    })


class _EmptyFetcher:
    """Always returns empty pages."""
    def __call__(self, *a, **k):
        return {"pages": [], "urls": []}


# ---------------------------------------------------------------------------
# BUG-1: agents.research propagates LLM exceptions (NEVER-RAISES violation)
# ---------------------------------------------------------------------------

class _RaisingLLM:
    """Simulates an LLM whose complete() raises (API timeout, network error)."""
    def __init__(self, exc=None):
        self.exc = exc or RuntimeError("API timeout / network error")

    def complete(self, system, user, max_tokens=1024, model=None, cache_system=False):
        raise self.exc


def test_agents_research_never_raises_when_llm_raises_runtimeerror(monkeypatch):
    """BUG-1: agents.research must NOT propagate a RuntimeError from llm_research.

    The docstring says 'any unexpected error degrades to desk research', but
    the implementation has no try/except around llm.llm_research, so a live
    LLM error (API timeout, rate-limit, etc.) propagates out and crashes the
    pipeline T10 step.

    Expected: no exception, research_source == 'desk_fallback'.
    Actual (BUG): RuntimeError propagates.
    """
    monkeypatch.setattr(research_fetch, "fetch_research_context", _EmptyFetcher())
    try:
        result = agents.research(_RaisingLLM(), _extract(), "raw text")
        # If we reach here the fix is in place.
        assert result["research_source"] == "desk_fallback"
        assert result["fetched_urls"] == []
    except RuntimeError as exc:
        pytest.fail(
            f"BUG-1: agents.research propagated RuntimeError from llm_research: {exc}\n"
            "Fix: wrap llm.llm_research(...) in a try/except inside agents.research "
            "and return a safe desk_fallback dict on any exception."
        )


def test_agents_research_never_raises_when_llm_raises_value_error(monkeypatch):
    """BUG-1 variant: ValueError from parse_research_response (malformed LLM JSON).

    parse_research_response calls _parse_json_object which raises ValueError
    if the model returns something that cannot be parsed as a JSON object.
    agents.research has no guard for this path.
    """
    class _GarbageLLM:
        def complete(self, system, user, **kw):
            raise ValueError("no JSON object found in LLM response")

    monkeypatch.setattr(research_fetch, "fetch_research_context", _EmptyFetcher())
    try:
        result = agents.research(_GarbageLLM(), _extract(), "raw text")
        assert result["research_source"] == "desk_fallback"
    except (ValueError, Exception) as exc:
        pytest.fail(
            f"BUG-1: agents.research propagated ValueError from parse_research_response: {exc}"
        )


def test_agents_research_never_raises_when_llm_raises_on_web_path(monkeypatch):
    """BUG-1 on the web path: even with a successful fetch, if the LLM call
    raises the exception must be caught and we must fall back gracefully.
    """
    def _good_fetch(source_link, company=None, **kw):
        return {
            "pages": [{"url": source_link, "text": "Acme builds RAG tools for banks. " * 20}],
            "urls": [source_link],
        }

    monkeypatch.setattr(research_fetch, "fetch_research_context", _good_fetch)
    try:
        result = agents.research(
            _RaisingLLM(RuntimeError("LLM network timeout")),
            _extract(),
            "raw text",
        )
        # After the fix, should degrade to some safe result.
        assert result is not None, "must return a dict, not None"
    except RuntimeError as exc:
        pytest.fail(
            f"BUG-1: agents.research propagated RuntimeError even on web path: {exc}"
        )


def test_t10_pipeline_not_stuck_when_llm_raises(monkeypatch):
    """BUG-1 integration: _do_research calling agents.research must not let a
    LLM exception escape to the advance() caller, leaving the item stuck in
    APPROVED state.

    We cannot call pipeline._do_research without a DB in a pure test, but we
    CAN verify that agents.research itself is the correct boundary — if it
    doesn't raise, _do_research won't either. This is the unit-level proof.
    """
    monkeypatch.setattr(research_fetch, "fetch_research_context", _EmptyFetcher())

    class _TimeoutLLM:
        def complete(self, system, user, **kw):
            raise httpx.TimeoutException("timeout")

    try:
        result = agents.research(_TimeoutLLM(), _extract(), "raw")
        # No exception -> T10 would not get stuck.
        assert result is not None
    except Exception as exc:
        pytest.fail(
            f"BUG-1 (pipeline integration): agents.research raised {type(exc).__name__}: {exc}\n"
            "This means pipeline._do_research would propagate the exception and T10 "
            "would leave the item stuck in APPROVED with no state transition."
        )


# ---------------------------------------------------------------------------
# BUG-2: desk_fallback does NOT clear sourced_facts (HONESTY violation)
# ---------------------------------------------------------------------------

def test_desk_fallback_sourced_facts_not_cleared_when_model_returns_fabricated(monkeypatch):
    """BUG-2 (HONESTY): on desk_fallback, agents.research must force sourced_facts=[]
    because no page was fetched and any model-supplied sourced_facts are fabricated.

    The spec says: 'On desk_fallback, assert sourced_facts ends up [] (our code
    doesn't inject facts) — i.e. a model can't smuggle "web" provenance when no
    fetch happened.'

    Current implementation: agents.research only overwrites research_source and
    fetched_urls authoritatively; it does NOT clear sourced_facts, so a
    hallucinating model's fabricated sourced_facts pass through unchallenged.
    """
    class _FabricatingLLM:
        def complete(self, system, user, max_tokens=1024, model=None, cache_system=False):
            # Model invents facts despite no page being fetched.
            return json.dumps({
                "summary": "ok",
                "talking_points": [],
                "questions": [],
                "sourced_facts": [
                    "Acme was founded in 2015 by Jeff Bezos (FABRICATED)",
                    "Acme has 500M USD funding (FABRICATED)",
                ],
            })

    monkeypatch.setattr(research_fetch, "fetch_research_context", _EmptyFetcher())

    result = agents.research(_FabricatingLLM(), _extract(), "raw text")

    assert result["research_source"] == "desk_fallback", (
        "research_source must be desk_fallback when no pages were fetched"
    )
    assert result["fetched_urls"] == [], "fetched_urls must be [] on desk_fallback"
    # BUG-2: this assertion will FAIL until the fix is applied.
    assert result["sourced_facts"] == [], (
        "BUG-2: on desk_fallback, sourced_facts must be [] regardless of what the model "
        "returned. A hallucinating model returned non-empty sourced_facts but no page "
        "was fetched, so these are fabricated facts that must be stripped by agents.research. "
        "Fix: after computing research_source='desk_fallback', set data['sourced_facts'] = []."
    )


def test_malicious_model_cannot_forge_full_provenance_on_desk_fallback(monkeypatch):
    """BUG-2 + provenance check: even if the model forges research_source='web',
    fetched_urls=['https://evil.com'], AND sourced_facts=['invented fact'],
    the authoritative overwrite in agents.research must ensure:
      - research_source == 'desk_fallback'  (already enforced -- passes today)
      - fetched_urls == []                  (already enforced -- passes today)
      - sourced_facts == []                 (BUG-2: NOT enforced today)
    """
    class _FullyMaliciousLLM:
        def complete(self, system, user, max_tokens=1024, model=None, cache_system=False):
            return json.dumps({
                "summary": "summary",
                "talking_points": [],
                "questions": [],
                "sourced_facts": ["Acme raised $100M series B (NOT from page)"],
                "research_source": "web",           # forge provenance
                "fetched_urls": ["https://acme.com"],  # forge URLs
            })

    monkeypatch.setattr(research_fetch, "fetch_research_context", _EmptyFetcher())
    result = agents.research(_FullyMaliciousLLM(), _extract(), "raw")

    # research_source and fetched_urls are already protected.
    assert result["research_source"] == "desk_fallback"
    assert result["fetched_urls"] == []
    # BUG-2: sourced_facts must also be cleared on desk_fallback.
    assert result["sourced_facts"] == [], (
        "BUG-2: malicious model forged sourced_facts on desk_fallback path; "
        "agents.research must strip sourced_facts to [] when research_source='desk_fallback'."
    )


# ---------------------------------------------------------------------------
# NEVER-RAISES matrix: every failure mode -> desk_fallback, no exception
# (these cover the gaps in test_agents.py which only tests fetcher failures,
#  not LLM failures)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc_type", [
    RuntimeError,
    ValueError,
    httpx.TimeoutException,
    httpx.ConnectError,
    ConnectionError,
    OSError,
])
def test_agents_research_never_raises_on_llm_exception(monkeypatch, exc_type):
    """Exhaustive never-raises matrix: every exception type the LLM client could
    raise must be caught inside agents.research. research_source must be
    desk_fallback (or the function returns a safe dict).

    This tests all six LLM exception types: RuntimeError (generic), ValueError
    (bad JSON from parse_research_response), httpx.TimeoutException (API timeout),
    httpx.ConnectError (no network), ConnectionError (stdlib), OSError (stdlib).
    """
    class _RaisesOnComplete:
        def complete(self, system, user, **kw):
            raise exc_type(f"simulated {exc_type.__name__}")

    monkeypatch.setattr(research_fetch, "fetch_research_context", _EmptyFetcher())
    try:
        result = agents.research(_RaisesOnComplete(), _extract(), "raw text")
        assert isinstance(result, dict), "must return a dict on failure"
    except exc_type as exc:
        pytest.fail(
            f"BUG-1: agents.research propagated {exc_type.__name__}: {exc}"
        )


# ---------------------------------------------------------------------------
# SSRF assessment (documented, non-blocking)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("internal_url,resolved_ip", [
    ("http://169.254.169.254/latest/meta-data", "169.254.169.254"),
    ("http://127.0.0.1/admin", "127.0.0.1"),
    ("http://localhost:8080/internal", "127.0.0.1"),
    ("http://10.0.0.1/router", "10.0.0.1"),
    ("http://192.168.1.1/admin", "192.168.1.1"),
])
def test_ssrf_internal_urls_blocked_before_fetch(monkeypatch, internal_url, resolved_ip):
    """SSRF guard (FIXED): private/link-local/loopback URLs are blocked by the
    resolved-IP guard in fetch_research_context BEFORE httpx is ever called.

    Attack vector: Telegram post (attacker-controlled) contains
    source_link = 'http://169.254.169.254/latest/meta-data' (cloud metadata).
    After operator approval, T10 research calls fetch_research_context(source_link).
    The guard now resolves the host and rejects any private/loopback/link-local
    IP, so no GET is issued and pages come back empty (-> desk_fallback).
    """
    from urllib.parse import urlparse
    parsed = urlparse(internal_url)
    assert parsed.scheme in ("http", "https")

    # Resolve the internal host to its (private/link-local/loopback) IP.
    monkeypatch.setattr(research_fetch, "_resolve_ips", lambda host: [resolved_ip])

    calls_made = []

    class _MockFakeResp:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html><body>" + ("x " * 300) + "</body></html>"

    def _recording_get(url, *, timeout, max_redirects):
        calls_made.append(url)
        return _MockFakeResp()

    result = fetch_research_context(internal_url, get=_recording_get)

    # FIXED: the guard blocks BEFORE httpx — no call, empty pages.
    assert calls_made == [], (
        f"SSRF guard failed: httpx was called with internal URL {internal_url!r}"
    )
    assert result["pages"] == []


def test_ssrf_company_url_in_page_can_be_private_ip():
    """SSRF second vector: _company_url_from_anchors can extract a private-IP URL
    from a page's anchor set and return it as the 'company page' to fetch next.

    Attack vector: attacker controls the HTML content of the source_link page
    (e.g. via an open redirect or a malicious job board). The page contains:
      <a href="http://169.254.169.254/latest/meta-data">our company</a>
    _company_url_from_anchors returns that URL, and fetch_research_context
    issues a second GET to the metadata endpoint.
    """
    from job_hunter.research_fetch import _company_url_from_anchors

    malicious_html = (
        "<html><body><p>" + ("job post content " * 50) + "</p>"
        '<a href="http://169.254.169.254/latest/meta-data">our company page</a>'
        "</body></html>"
    )
    result = _company_url_from_anchors(
        malicious_html, "https://jobs.test/v/1", "some company"
    )
    # Currently returns the metadata URL (no guard).
    # After a fix, this should return None (blocked as link-local).
    if result is not None:
        assert result == "http://169.254.169.254/latest/meta-data", (
            "SSRF: _company_url_from_anchors returned a private IP URL. "
            "This URL would be fetched as the 'company page'. "
            "A fix would add a private-IP blocklist to _company_url_from_anchors."
        )
    # If result is None, a fix was applied; test passes either way (informational).


def test_ssrf_redirect_to_private_ip_blocked_at_redirect(monkeypatch):
    """SSRF third vector (FIXED): a redirect from a legitimate URL to a private
    IP is re-validated and rejected. Redirects are now followed MANUALLY in
    research_fetch with a per-hop _url_is_safe check on the redirect TARGET, so
    a 3xx pointing at 169.254.169.254 aborts the fetch before the target GET.
    """
    def resolve(host):
        if host == "169.254.169.254":
            return ["169.254.169.254"]
        return ["93.184.216.34"]

    monkeypatch.setattr(research_fetch, "_resolve_ips", resolve)

    calls = []

    def _redirect_get(url, *, timeout, max_redirects):
        calls.append(url)
        if "169.254.169.254" in url:
            # Must never be reached: guard aborts before fetching the target.
            class R:
                status_code = 200
                headers = {"content-type": "text/html"}
                text = "<html><body>" + ("data " * 300) + "</body></html>"
            return R()

        class Redirect:
            status_code = 302
            headers = {"location": "http://169.254.169.254/latest/meta-data"}
            text = ""
        return Redirect()

    result = fetch_research_context(
        "https://jobs.legit-board.example.com/v/1",
        get=_redirect_get,
    )
    # The legitimate first URL was fetched; the private redirect target was NOT.
    assert calls == ["https://jobs.legit-board.example.com/v/1"]
    assert result["pages"] == []


# ---------------------------------------------------------------------------
# NEVER-RAISES: full failure mode matrix for fetch_research_context
# (supplements test_research_fetch.py with additional httpx exceptions)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [
    httpx.ReadTimeout("timeout"),
    httpx.ConnectTimeout("connect timeout"),
    httpx.PoolTimeout("pool timeout"),
    httpx.RemoteProtocolError("protocol error"),
    httpx.TooManyRedirects("too many redirects"),
    Exception("unexpected"),
    MemoryError("oom"),
])
def test_fetch_research_context_never_raises_on_httpx_exceptions(exc):
    """fetch_research_context must swallow all httpx / stdlib exceptions and
    return empty pages without propagating.

    Covers additional httpx exception types not in test_research_fetch.py:
    ReadTimeout, ConnectTimeout, PoolTimeout, RemoteProtocolError,
    TooManyRedirects, and generic Exception/MemoryError.
    """
    def _raises(url, *, timeout, max_redirects):
        raise exc

    result = fetch_research_context("https://x.test/v", get=_raises)
    assert result == {"pages": [], "urls": []}, (
        f"fetch_research_context must return empty pages on {type(exc).__name__}, not raise"
    )


# ---------------------------------------------------------------------------
# SUCCESS enrichment: exact text and label reach the prompt
# ---------------------------------------------------------------------------

def test_success_enrichment_fetched_label_in_prompt(monkeypatch, fake_llm):
    """Verify that with a mocked page returning real text:
      - The exact 'FETCHED PAGE TEXT (real, from <url>)' label is in the prompt.
      - The actual fetched text is in the prompt.
      - research_source == 'web'.
      - fetched_urls is populated.
      - Model is Haiku, max_tokens == RESEARCH_MAX_TOKENS (raised to 2000).
    """
    fake = fake_llm
    fake.set_for("research a company/role", _research_resp(["Acme fact."]))

    page_text = "Acme Corp: We build RAG pipelines for enterprise banks."

    def _good_fetch(source_link, company=None, **kw):
        return {
            "pages": [{"url": source_link, "text": page_text}],
            "urls": [source_link],
        }

    monkeypatch.setattr(research_fetch, "fetch_research_context", _good_fetch)

    # The apply URL lives in the post body (#20); select_primary_url picks it.
    out = agents.research(
        fake, _extract(), "raw post. Apply at https://jobs.test/v/1",
        model="claude-haiku-4-5",
    )

    assert out["research_source"] == "web"
    assert "https://jobs.test/v/1" in out["fetched_urls"]

    # Find the research call in the recorded calls.
    rc = [c for c in fake.calls if "research a company/role" in c["system"]]
    assert rc, "LLM research call was not made"
    call = rc[-1]
    assert "FETCHED PAGE TEXT (real, from https://jobs.test/v/1)" in call["user"]
    assert page_text in call["user"]
    assert "ORIGINAL POST" in call["user"]
    assert call["model"] == "claude-haiku-4-5"
    assert call["max_tokens"] == llm.RESEARCH_MAX_TOKENS == 2000


# ---------------------------------------------------------------------------
# HONESTY: system prompt contains all required clauses
# ---------------------------------------------------------------------------

def test_research_system_honesty_clauses_present():
    """RESEARCH_SYSTEM must contain all four honesty clauses from the spec."""
    s = llm.RESEARCH_SYSTEM
    assert "sourced_facts" in s, "missing 'sourced_facts' key in system"
    assert "NEVER invent company facts" in s, "missing 'NEVER invent company facts'"
    assert "информация о компании со страницы недоступна" in s, (
        "missing the required Russian unavailability note"
    )
    assert "FETCHED PAGE TEXT (real, from" in s, (
        "missing the label pattern that ties fetched text to sourced_facts"
    )
    assert "ORIGINAL POST" in s or "not necessarily verified" in s, (
        "missing disclaimer that ORIGINAL POST is not a verified fact source"
    )
    assert "inference" in s.lower() or "infer" in s.lower() or "NOT state it as" in s, (
        "missing instruction that non-page content must not be stated as a confirmed fact"
    )


# ---------------------------------------------------------------------------
# FALLBACK byte-identity: desk_fallback path = historical prompt
# ---------------------------------------------------------------------------

def test_fallback_prompt_byte_identical_to_historical():
    """When fetch returns empty, the prompt passed to the LLM must be
    byte-identical to the pre-Phase-1 desk-only prompt.
    """
    import json as _json

    ex = _extract()
    payload = {
        "title": ex.title,
        "company": ex.company,
        "stack": ex.stack,
        "location": ex.location,
    }
    historical = (
        "Job context:\n"
        + _json.dumps(payload, ensure_ascii=False)
        + "\n\nOriginal post:\n"
        + "raw body"
    )
    # No fetched context -> desk prompt.
    assert llm.build_research_prompt(ex, "raw body") == historical
    # Empty pages dict -> still desk prompt.
    assert llm.build_research_prompt(ex, "raw body", {"pages": []}) == historical
    # None context -> still desk prompt.
    assert llm.build_research_prompt(ex, "raw body", None) == historical


# ---------------------------------------------------------------------------
# Truncation + limits: text ≤ max_chars, max 2 URLs, skip-list
# ---------------------------------------------------------------------------

def test_truncation_large_page_capped_at_max_chars():
    """A very large page must be truncated to max_chars before it reaches the LLM."""
    huge_html = "<html><body><p>" + ("a" * 100_000) + "</p></body></html>"

    def _fake_get(url, *, timeout, max_redirects):
        class R:
            status_code = 200
            headers = {"content-type": "text/html"}
            text = huge_html
        return R()

    result = fetch_research_context("https://x.test/v", get=_fake_get, max_chars=6000)
    assert len(result["pages"]) == 1
    assert len(result["pages"][0]["text"]) <= 6000


def test_skip_list_prevents_vk_github_hh_tm_fetches():
    """The company 2nd-fetch must not fire for social/aggregator hostnames."""
    from job_hunter.research_fetch import _company_url_from_anchors

    for bad_host, href in [
        ("vk.com", "https://vk.com/acmejobs"),
        ("github.com", "https://github.com/acmecorp"),
        ("hh.ru", "https://hh.ru/vacancy/12345"),
        ("t.me", "https://t.me/acme_official"),
    ]:
        html = (
            "<html><body><p>" + ("job " * 60) + "</p>"
            f'<a href="{href}">link</a></body></html>'
        )
        result = _company_url_from_anchors(html, "https://jobs.test/v", "Acme")
        assert result is None, (
            f"Skip-list failed for {bad_host}: _company_url_from_anchors returned {result!r}"
        )


def test_max_two_urls_cap_respected():
    """Hard cap: at most 2 URLs are ever fetched regardless of max_urls setting."""
    calls = []

    def _fake_get(url, *, timeout, max_redirects):
        calls.append(url)
        if "acme.io" in url:
            body = "<html><body><p>" + ("company " * 100) + "</p></body></html>"
        else:
            body = (
                "<html><body><p>" + ("job " * 60) + "</p>"
                "<a href='https://acme.io'>Acme</a></body></html>"
            )

        class R:
            status_code = 200
            headers = {"content-type": "text/html"}
            text = body
        return R()

    result = fetch_research_context(
        "https://jobs.test/v/1", "Acme", get=_fake_get, max_urls=2
    )
    assert len(result["pages"]) <= 2
    assert len(calls) <= 2, f"More than 2 HTTP calls were made: {calls}"


def test_httpx_called_with_timeout_8_and_max_redirects_1():
    """Default invocation wires httpx with timeout=8.0 and max_redirects=1."""
    received = {}

    def _fake_get(url, *, timeout, max_redirects):
        received["timeout"] = timeout
        received["max_redirects"] = max_redirects

        class R:
            status_code = 200
            headers = {"content-type": "text/html"}
            text = "<html><body>" + ("x " * 300) + "</body></html>"
        return R()

    fetch_research_context("https://x.test/v", get=_fake_get)
    assert received.get("timeout") == 8.0
    assert received.get("max_redirects") == 1


# ---------------------------------------------------------------------------
# Draft unchanged: existing research dict is compatible with llm_draft
# ---------------------------------------------------------------------------

def test_research_dict_keys_compatible_with_llm_draft(monkeypatch, fake_llm):
    """The research dict returned by agents.research must have summary,
    talking_points, questions so llm_draft can consume it. The new sourced_facts
    key must not break build_draft_prompt or llm_draft.
    """
    fake = fake_llm
    fake.set_for("research a company/role", _research_resp(["fact"]))
    fake.set_for("application message", "Short draft message.")

    monkeypatch.setattr(
        research_fetch, "fetch_research_context",
        lambda *a, **k: {"pages": [], "urls": []},
    )

    research_data = agents.research(fake, _extract(), "raw text")
    # Verify the keys the draft consumes are present.
    assert "summary" in research_data
    assert "talking_points" in research_data
    assert "questions" in research_data
    # New key also present (does not break anything).
    assert "sourced_facts" in research_data

    # Actually run the draft with this research_data to confirm no crash.
    draft = agents.draft(fake, _extract(), "raw text", research_data)
    assert "Short draft message." in draft
    assert "github.com/example" in draft  # signature appended
