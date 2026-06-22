"""Adversarial tests for Phase 1.5 (#20): in-body URL selection.

Added by the Tester validation pass. Covers gaps identified against the
VALIDATE requirements in the issue:

  GAP 1: _is_telegram_host() is not directly unit-tested — no coverage for
    lookalike domains (supert.me, nott.me, start.me) that must NOT be excluded,
    nor for the exact matching rule (suffix, not substring).

  GAP 2: contact_type != "link" (dm/form/None) with a URL-shaped contact is
    not explicitly tested — only the contact_type="link" path is covered.
    The spec requires that contact_type="dm"/"form"/None NEVER causes the
    contact field to be used as the primary URL, even when the contact
    value looks like a URL.

  GAP 3: SSRF guard on the in-body URL, tested at the fetch_research_context
    level (without the agents layer), to confirm that select_primary_url picks
    the evil URL and fetch_research_context blocks it before any GET.

  GAP 4: contact field with trailing prose punctuation (period, comma) is
    trimmed correctly — the strip logic applies to the contact field too.

  GAP 5: Multiple non-telegram URLs in body — first is chosen regardless of
    host, confirming ordering not host-ranking.

No real network / DNS. The autouse fixture maps every host to a public IP;
individual tests override _resolve_ips for SSRF scenarios.
"""

from __future__ import annotations

import pytest

from job_hunter import research_fetch
from job_hunter.research_fetch import (
    _is_telegram_host,
    select_primary_url,
    fetch_research_context,
)
from job_hunter.schema_extract import ExtractResult


_TG = "https://t.me/somechan/1234"


def _ex(**kw):
    base = dict(title="Engineer", source_channel="@chan")
    base.update(kw)
    return ExtractResult(**base)


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Every host resolves to a public IP by default; SSRF tests override."""
    monkeypatch.setattr(research_fetch, "_resolve_ips", lambda host: ["93.184.216.34"])


# ---------------------------------------------------------------------------
# GAP 1: _is_telegram_host() unit tests — matching rule verification
# ---------------------------------------------------------------------------

class TestIsTelegramHost:
    """Direct unit tests for _is_telegram_host().

    The matching rule is: h == tg OR h.endswith("." + tg) for each tg in
    _TELEGRAM_HOSTS. This is a SUFFIX (subdomain) check, NOT a naive substring
    check. These tests document and guard that invariant.
    """

    # --- Exact matches must be excluded ---

    @pytest.mark.parametrize("host", [
        "t.me",
        "T.ME",
        "t.Me",
        "telegram.org",
        "TELEGRAM.ORG",
        "telegram.me",
        "TELEGRAM.ME",
    ])
    def test_exact_telegram_hosts_excluded(self, host):
        """Exact telegram hosts (all case variants) are excluded."""
        assert _is_telegram_host(host) is True, (
            f"_is_telegram_host({host!r}) should be True (exact match)"
        )

    # --- Subdomain matches must be excluded ---

    @pytest.mark.parametrize("host", [
        "www.t.me",
        "foo.t.me",
        "www.telegram.org",
        "sub.telegram.me",
        "deep.sub.t.me",
    ])
    def test_subdomain_telegram_hosts_excluded(self, host):
        """Subdomains of telegram hosts are excluded (proper suffix match)."""
        assert _is_telegram_host(host) is True, (
            f"_is_telegram_host({host!r}) should be True (subdomain match)"
        )

    # --- Lookalike domains must NOT be excluded (suffix not substring) ---

    @pytest.mark.parametrize("host,description", [
        ("supert.me", "ends with 't.me' as chars but 'supert' is not a subdomain of 't.me'"),
        ("nott.me", "contains 't.me' as suffix chars but not subdomain"),
        ("start.me", "ends with 't.me' at char level but no dot before 't.me'"),
        ("faket.me", "contains 't.me' at char level but not a subdomain"),
        ("notatelegram.org", "contains 'telegram.org' chars but no dot separator"),
        ("faketelgram.me", "similar spelling but different TLD structure"),
    ])
    def test_lookalike_hosts_not_excluded(self, host, description):
        """Lookalike domains that end with telegram chars but are NOT subdomains
        must NOT be excluded. The check is suffix (subdomain), not substring.

        If this test fails with _is_telegram_host returning True, it means the
        matching is naive substring ('t.me' in host) instead of proper suffix
        (host.endswith('.t.me') or host == 't.me'). That would be a false-exclude
        bug affecting legitimate hosts like start.me.
        """
        assert _is_telegram_host(host) is False, (
            f"_is_telegram_host({host!r}) should be False: {description}\n"
            "BUG: if True, the match is naive substring, not proper suffix/exact."
        )

    # --- Attacker-constructed hosts must NOT be excluded ---

    @pytest.mark.parametrize("host,description", [
        ("t.me.attacker.com", "t.me is a label, not the TLD; host is attacker.com"),
        ("not-t.me.evil.com", "t.me appears mid-hostname"),
        ("mytelegram.org.example.com", "telegram.org appears as label in another domain"),
        ("xtelegram.me", "starts with telegram-like chars but not a subdomain"),
    ])
    def test_attacker_constructed_hosts_not_excluded(self, host, description):
        """Attacker-controlled hosts that embed telegram domain names as labels
        must NOT be excluded as if they were telegram hosts.

        If _is_telegram_host returns True for these, it means the rule would
        incorrectly treat a legitimate attacker-domain as a 'telegram' host and
        skip it — which is the WRONG direction (false-exclude, not false-include).
        """
        assert _is_telegram_host(host) is False, (
            f"_is_telegram_host({host!r}) should be False: {description}"
        )

    # --- Edge cases ---

    def test_none_host_returns_false(self):
        """None host must return False without raising."""
        assert _is_telegram_host(None) is False

    def test_empty_string_host_returns_false(self):
        """Empty string host must return False without raising."""
        assert _is_telegram_host("") is False

    def test_trailing_dot_ignored(self):
        """A trailing dot (FQDN form) is stripped before comparison."""
        assert _is_telegram_host("t.me.") is True
        assert _is_telegram_host("www.t.me.") is True

    def test_matching_is_suffix_not_substring(self):
        """Explicitly document that 'supert.me' is NOT excluded while 'www.t.me' IS.

        This is the critical invariant: the match must be exact-or-subdomain,
        NOT naive 't.me' in host.
        """
        # 'www.t.me' has a dot before 't.me' -> it IS a subdomain -> excluded
        assert _is_telegram_host("www.t.me") is True

        # 'supert.me' does NOT have a dot before 't.me' -> not a subdomain
        # A naive 'in' check would make this True (false exclude)
        assert _is_telegram_host("supert.me") is False, (
            "BUG (false-exclude): 'supert.me' is not a telegram subdomain. "
            "If this fails, the match is naive substring 't.me' in host, "
            "which would incorrectly block legitimate hosts like start.me."
        )


# ---------------------------------------------------------------------------
# GAP 2: contact_type != "link" must NEVER use contact as primary URL
# ---------------------------------------------------------------------------

class TestSelectContactTypeNonLink:
    """contact_type != "link" means contact is NOT a link target, even if its
    value happens to look like a URL. The spec requires strict equality check:
    only contact_type == "link" qualifies for the contact-as-primary-URL path.
    """

    def test_contact_type_dm_with_url_contact_falls_to_raw_text(self):
        """contact_type='dm' with a URL contact -> contact NOT selected;
        falls through to raw_text scan."""
        ex = _ex(contact_type="dm", contact="https://company.example/jobs",
                 source_link=_TG)
        r = select_primary_url(ex, "Apply: https://hh.ru/vacancy/1")
        assert r == "https://hh.ru/vacancy/1", (
            f"contact_type='dm' with URL contact must fall to raw_text; got {r!r}"
        )

    def test_contact_type_form_with_url_contact_falls_to_raw_text(self):
        """contact_type='form' with a URL contact -> contact NOT selected."""
        ex = _ex(contact_type="form", contact="https://company.example/apply",
                 source_link=_TG)
        r = select_primary_url(ex, "Apply: https://hh.ru/vacancy/2")
        assert r == "https://hh.ru/vacancy/2", (
            f"contact_type='form' with URL contact must fall to raw_text; got {r!r}"
        )

    def test_contact_type_none_with_url_contact_falls_to_raw_text(self):
        """contact_type=None with a URL contact -> contact NOT selected.
        None != 'link', so the contact path is skipped."""
        ex = _ex(contact_type=None, contact="https://company.example/jobs",
                 source_link=_TG)
        r = select_primary_url(ex, "Apply: https://hh.ru/vacancy/3")
        assert r == "https://hh.ru/vacancy/3", (
            f"contact_type=None with URL contact must fall to raw_text; got {r!r}"
        )

    def test_contact_type_dm_with_at_handle_falls_to_raw_text(self):
        """contact_type='dm', contact='@recruiter' -> @handle is not an http(s)
        URL anyway, but the type check fires first. raw_text is scanned."""
        ex = _ex(contact_type="dm", contact="@recruiter", source_link=_TG)
        r = select_primary_url(ex, "Apply: https://hh.ru/vacancy/4")
        assert r == "https://hh.ru/vacancy/4", (
            f"@handle contact with type='dm' must fall to raw_text; got {r!r}"
        )

    def test_contact_type_dm_with_email_falls_to_raw_text(self):
        """contact_type='dm', contact='hr@company.com' -> email is not an http(s)
        URL, and type='dm' != 'link'. raw_text is scanned."""
        ex = _ex(contact_type="dm", contact="hr@company.com", source_link=_TG)
        r = select_primary_url(ex, "Apply: https://hh.ru/vacancy/5")
        assert r == "https://hh.ru/vacancy/5", (
            f"email contact with type='dm' must fall to raw_text; got {r!r}"
        )

    def test_contact_type_dm_with_url_and_no_raw_text_url_returns_none(self):
        """contact_type='dm' with URL, no usable URL in raw_text -> None."""
        ex = _ex(contact_type="dm", contact="https://company.example/jobs",
                 source_link=_TG)
        r = select_primary_url(ex, "DM us at @recruiter, no link")
        assert r is None, (
            f"contact_type='dm' URL + no raw_text URL must return None; got {r!r}"
        )

    def test_contact_type_link_empty_string_falls_to_raw_text(self):
        """contact_type='link' but contact='' -> empty contact, falls to raw_text."""
        ex = _ex(contact_type="link", contact="", source_link=_TG)
        r = select_primary_url(ex, "Apply: https://hh.ru/vacancy/6")
        assert r == "https://hh.ru/vacancy/6", (
            f"contact_type='link' with empty contact must fall to raw_text; got {r!r}"
        )

    def test_contact_type_link_non_http_scheme_falls_to_raw_text(self):
        """contact_type='link' but contact has no http(s) scheme -> falls to raw_text."""
        ex = _ex(contact_type="link", contact="company.example/jobs", source_link=_TG)
        r = select_primary_url(ex, "Apply: https://hh.ru/vacancy/7")
        assert r == "https://hh.ru/vacancy/7", (
            f"contact_type='link' with schemeless URL must fall to raw_text; got {r!r}"
        )

    def test_contact_type_link_telegram_url_falls_to_raw_text(self):
        """contact_type='link' with a t.me URL contact -> telegram excluded,
        falls to raw_text. (Already in test_research_fetch.py but kept here
        for completeness of the type-matrix.)"""
        ex = _ex(contact_type="link", contact="https://t.me/recruiter",
                 source_link=_TG)
        r = select_primary_url(ex, "Apply: https://hh.ru/vacancy/8")
        assert r == "https://hh.ru/vacancy/8", (
            f"telegram contact with type='link' must fall to raw_text; got {r!r}"
        )

    def test_contact_type_dm_both_telegram_contact_and_only_telegram_in_body_returns_none(self):
        """contact_type='dm' + telegram-shaped contact, body has only t.me URL
        -> contact skipped (type != link), body has only telegram -> None."""
        ex = _ex(contact_type="dm", contact="https://t.me/chan", source_link=_TG)
        r = select_primary_url(ex, f"Channel: {_TG}")
        assert r is None, (
            f"All telegram paths must yield None; got {r!r}"
        )


# ---------------------------------------------------------------------------
# GAP 3: SSRF guard on in-body URL, tested at fetch_research_context level
# ---------------------------------------------------------------------------

class TestSSRFOnInBodyURL:
    """The SSRF guard must fire on the URL that select_primary_url returns
    (the in-body apply URL), not just on source_link (the old Phase 1 behavior).

    These tests go directly through fetch_research_context so they verify the
    guard at the fetch layer, independent of the agents layer.
    """

    def test_in_body_url_resolving_to_cloud_metadata_blocked(self, monkeypatch):
        """An in-body URL whose host resolves to 169.254.169.254 (cloud metadata)
        is blocked by the SSRF guard inside fetch_research_context -> empty pages."""
        monkeypatch.setattr(
            research_fetch, "_resolve_ips",
            lambda h: ["169.254.169.254"] if h == "evil.example" else ["93.184.216.34"]
        )
        called = []

        def fake_get(u, *, timeout, max_redirects):
            called.append(u)
            class R:
                status_code = 200
                headers = {"content-type": "text/html"}
                text = "<html><body>" + ("x " * 300) + "</body></html>"
            return R()

        # select_primary_url would pick this URL from the body text
        in_body_url = "http://evil.example/apply"
        result = fetch_research_context(in_body_url, "Acme", get=fake_get)

        assert result == {"pages": [], "urls": []}, (
            "In-body URL resolving to cloud-metadata IP must be blocked"
        )
        assert called == [], "fetch must NOT have been attempted for SSRF-blocked URL"

    def test_in_body_url_resolving_to_rfc1918_blocked(self, monkeypatch):
        """An in-body URL whose host resolves to an RFC-1918 IP is blocked."""
        monkeypatch.setattr(
            research_fetch, "_resolve_ips",
            lambda h: ["10.0.0.5"] if h == "internal.example" else ["93.184.216.34"]
        )
        called = []

        def fake_get(u, *, timeout, max_redirects):
            called.append(u)
            class R:
                status_code = 200
                headers = {"content-type": "text/html"}
                text = "<html><body>" + ("x " * 300) + "</body></html>"
            return R()

        result = fetch_research_context(
            "https://internal.example/vacancy", "Acme", get=fake_get
        )
        assert result["pages"] == []
        assert called == []

    def test_in_body_url_redirect_to_private_ip_blocked(self, monkeypatch):
        """Redirect from the in-body URL to a private IP is blocked at the
        redirect hop (not at the initial fetch)."""
        def resolve(host):
            if host == "169.254.169.254":
                return ["169.254.169.254"]
            return ["93.184.216.34"]

        monkeypatch.setattr(research_fetch, "_resolve_ips", resolve)
        seen = []

        def fake_get(u, *, timeout, max_redirects):
            seen.append(u)
            if "169.254" in u:
                class R:
                    status_code = 200
                    headers = {"content-type": "text/html"}
                    text = "<html><body>" + ("secret " * 300) + "</body></html>"
                return R()
            class Redirect:
                status_code = 302
                headers = {"location": "http://169.254.169.254/latest/meta-data"}
                text = ""
            return Redirect()

        result = fetch_research_context(
            "https://public-apply.example/vacancy", "Acme", get=fake_get
        )
        assert result["pages"] == []
        assert "http://169.254.169.254" not in seen, (
            "Private redirect target must NOT be fetched"
        )


# ---------------------------------------------------------------------------
# GAP 4: Trailing punctuation on contact field
# ---------------------------------------------------------------------------

class TestContactFieldTrailingPunctuation:
    """The _strip_url_trailing function is applied to the contact field when
    contact_type='link'. Trailing prose punctuation must be removed."""

    def test_contact_with_trailing_period_stripped(self):
        """contact='https://company.example/jobs.' -> period stripped."""
        ex = _ex(contact_type="link", contact="https://company.example/jobs.",
                 source_link=_TG)
        r = select_primary_url(ex, "fallback https://hh.ru/v/1")
        assert r == "https://company.example/jobs", (
            f"Trailing period must be stripped from contact field; got {r!r}"
        )

    def test_contact_with_trailing_comma_stripped(self):
        """contact='https://company.example/jobs,' -> comma stripped."""
        ex = _ex(contact_type="link", contact="https://company.example/jobs,",
                 source_link=_TG)
        r = select_primary_url(ex, "fallback https://hh.ru/v/1")
        assert r == "https://company.example/jobs", (
            f"Trailing comma must be stripped from contact field; got {r!r}"
        )

    def test_contact_with_trailing_closing_paren_stripped(self):
        """contact='https://company.example/jobs)' -> paren stripped."""
        ex = _ex(contact_type="link", contact="https://company.example/jobs)",
                 source_link=_TG)
        r = select_primary_url(ex, "fallback https://hh.ru/v/1")
        assert r == "https://company.example/jobs", (
            f"Trailing ) must be stripped from contact field; got {r!r}"
        )

    def test_contact_with_query_string_preserved(self):
        """contact with a real query string -> query string kept intact."""
        ex = _ex(contact_type="link",
                 contact="https://company.example/apply?from=telegram&role=ai",
                 source_link=_TG)
        r = select_primary_url(ex, "fallback https://hh.ru/v/1")
        assert r == "https://company.example/apply?from=telegram&role=ai", (
            f"Query string must be preserved in contact field; got {r!r}"
        )


# ---------------------------------------------------------------------------
# GAP 5: Multiple non-telegram URLs in body — ordering
# ---------------------------------------------------------------------------

class TestRawTextURLOrdering:
    """The FIRST non-telegram http(s) URL in raw_text is chosen, not the
    'best' or most relevant one. This tests ordering behavior."""

    def test_first_url_chosen_when_multiple_non_telegram_urls(self):
        """Body has two non-telegram URLs -> first is chosen."""
        ex = _ex(source_link=_TG)
        r = select_primary_url(ex, "Apply: https://hh.ru/v/1 or https://github.com/x")
        assert r == "https://hh.ru/v/1", (
            f"First non-telegram URL must be chosen; got {r!r}"
        )

    def test_telegram_skipped_first_non_telegram_returned(self):
        """Body: t.me first, then hh.ru -> t.me skipped, hh.ru chosen."""
        ex = _ex(source_link=_TG)
        r = select_primary_url(ex, f"{_TG} apply at https://hh.ru/vacancy/123")
        assert r == "https://hh.ru/vacancy/123", (
            f"t.me must be skipped; first non-telegram chosen; got {r!r}"
        )

    def test_multiple_telegram_urls_then_non_telegram_returns_non_telegram(self):
        """Body: two t.me URLs then one hh.ru -> hh.ru chosen."""
        ex = _ex(source_link=_TG)
        raw = (
            "Join: https://t.me/chan/1 "
            "Channel: https://t.me/chan/2 "
            "Apply: https://hh.ru/vacancy/99"
        )
        r = select_primary_url(ex, raw)
        assert r == "https://hh.ru/vacancy/99", (
            f"All t.me skipped; hh.ru chosen; got {r!r}"
        )

    def test_only_telegram_urls_returns_none(self):
        """Body has only t.me URLs -> None."""
        ex = _ex(source_link=_TG)
        raw = "Join: https://t.me/chan/1 Channel: https://telegram.org/blog"
        r = select_primary_url(ex, raw)
        assert r is None, (
            f"All-telegram body must return None; got {r!r}"
        )

    def test_non_http_tokens_like_www_dot_foo_dot_com_not_matched(self):
        """www.foo.com without http(s) scheme is not matched by _URL_RE."""
        ex = _ex(source_link=_TG)
        r = select_primary_url(ex, "Visit www.company.example for details")
        assert r is None, (
            f"Non-http(s) token must not be selected; got {r!r}"
        )

    def test_url_with_path_query_fragment_kept_whole(self):
        """A URL with path/query/fragment in raw_text is returned intact."""
        ex = _ex(source_link=_TG)
        full_url = "https://jobs.example.com/vacancy/123?from=tg&ref=bot#apply"
        r = select_primary_url(ex, f"Apply: {full_url}")
        assert r == full_url, (
            f"URL with query+fragment must be returned whole; got {r!r}"
        )

    def test_raw_text_empty_string_returns_none(self):
        """Empty raw_text returns None."""
        ex = _ex(source_link=_TG)
        assert select_primary_url(ex, "") is None

    def test_raw_text_none_returns_none(self):
        """raw_text=None returns None (never raises)."""
        ex = _ex(source_link=_TG)
        assert select_primary_url(ex, None) is None

    def test_raw_text_garbage_bytes_returns_none(self):
        """Garbage binary-like raw_text returns None without raising."""
        ex = _ex(source_link=_TG)
        assert select_primary_url(ex, "\x00\xff\x01garbage!!!") is None
