"""Public t.me/s/ web reader: pure HTML parse + I/O wrapper with mocked HTTP.

No real network calls anywhere in this module. The pure parser is fed a
realistic captured t.me/s/ HTML snippet; the httpx fetch is mocked via the
injectable ``http_get`` hook (mirroring tests/test_fx.py).
"""

from job_hunter import ingest_web as web
from job_hunter import store
from job_hunter.config import Config

# A realistic, trimmed t.me/s/ preview snippet (two message blocks, one empty
# service block that must be skipped). Mirrors Telegram's real markup:
# wrapper carries data-post="<channel>/<id>", text lives in
# tgme_widget_message_text, permalink/time in tgme_widget_message_date.
SAMPLE_HTML = """
<!DOCTYPE html>
<html><body>
<section class="tgme_channel_history js-message_history">

  <div class="tgme_widget_message_wrap js-widget_message_wrap">
    <div class="tgme_widget_message text_not_supported_wrap js-widget_message"
         data-post="jobschan/101" data-view="x">
      <div class="tgme_widget_message_text js-message_text" dir="auto">
        Senior Python Engineer (Remote)<br>
        Stack: Python, FastAPI, RAG, Claude API.<br>
        Contact: <a href="https://t.me/hr_person">@hr_person</a>
      </div>
      <div class="tgme_widget_message_footer">
        <a class="tgme_widget_message_date" href="https://t.me/jobschan/101?single">
          <time datetime="2026-06-20T10:00:00+00:00" class="time">10:00</time>
        </a>
      </div>
    </div>
  </div>

  <div class="tgme_widget_message_wrap js-widget_message_wrap">
    <div class="tgme_widget_message js-widget_message" data-post="jobschan/102">
      <div class="tgme_widget_message_text js-message_text" dir="auto">
        Middle ML/LLM &amp; prompt eval role. Salary 250000 RUB. Remote OK.
      </div>
      <a class="tgme_widget_message_date" href="https://t.me/jobschan/102">
        <time datetime="2026-06-20T11:30:00+00:00">11:30</time>
      </a>
    </div>
  </div>

  <!-- service/empty block: no text, must be skipped gracefully -->
  <div class="tgme_widget_message_wrap js-widget_message_wrap">
    <div class="tgme_widget_message js-widget_message" data-post="jobschan/103">
      <div class="tgme_widget_message_sticker_wrap"></div>
      <a class="tgme_widget_message_date" href="https://t.me/jobschan/103">
        <time datetime="2026-06-20T12:00:00+00:00">12:00</time>
      </a>
    </div>
  </div>

</section>
</body></html>
"""


# --------------------------------------------------------------------------
# PURE parser
# --------------------------------------------------------------------------

def test_public_channel_url_pure():
    assert web.public_channel_url("@jobschan") == "https://t.me/s/jobschan"
    assert web.public_channel_url("jobschan") == "https://t.me/s/jobschan"
    assert web.public_channel_url("https://t.me/jobschan") == "https://t.me/s/jobschan"
    assert web.public_channel_url("https://t.me/s/jobschan") == "https://t.me/s/jobschan"


def test_parse_channel_html_extracts_text_id_permalink():
    msgs = web.parse_channel_html(SAMPLE_HTML, "@jobschan")
    # Empty sticker-only block (103) is dropped.
    assert [m.source_message_id for m in msgs] == ["101", "102"]

    first = msgs[0]
    assert first.source_channel == "@jobschan"
    assert first.source_message_id == "101"
    assert first.source_link == "https://t.me/jobschan/101"
    # <br> tags become newlines; anchor text is preserved; HTML stripped.
    assert "Senior Python Engineer (Remote)" in first.raw_text
    assert "Python, FastAPI, RAG, Claude API." in first.raw_text
    assert "@hr_person" in first.raw_text
    assert "<a" not in first.raw_text and "<br" not in first.raw_text

    second = msgs[1]
    assert second.source_message_id == "102"
    assert second.source_link == "https://t.me/jobschan/102"
    # HTML entity decoded.
    assert "Middle ML/LLM & prompt eval role." in second.raw_text
    assert "Salary 250000 RUB" in second.raw_text


def test_parse_channel_html_empty_and_garbage():
    assert web.parse_channel_html("", "@c") == []
    assert web.parse_channel_html("<html><body>no messages</body></html>", "@c") == []


def test_parse_channel_html_skips_blocks_without_id():
    html = (
        '<div class="tgme_widget_message js-widget_message">'
        '<div class="tgme_widget_message_text">orphan no data-post</div>'
        "</div>"
    )
    assert web.parse_channel_html(html, "@c") == []


# --------------------------------------------------------------------------
# I/O wrapper with MOCKED httpx (no network)
# --------------------------------------------------------------------------

class FakeResp:
    def __init__(self, text, status_ok=True):
        self.text = text
        self._ok = status_ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("http error")


def test_fetch_channel_html_uses_injected_get():
    seen = {}

    def fake_get(url):
        seen["url"] = url
        return FakeResp(SAMPLE_HTML)

    html = web.fetch_channel_html("@jobschan", http_get=fake_get)
    assert seen["url"] == "https://t.me/s/jobschan"
    assert "tgme_widget_message" in html


def test_web_reader_fetch_channel_parses():
    reader = web.WebReader(http_get=lambda url: FakeResp(SAMPLE_HTML))
    msgs = reader.fetch_channel("@jobschan")
    assert [m.source_message_id for m in msgs] == ["101", "102"]


def test_ingest_stores_and_dedups(conn):
    cfg = Config(telegram_channels=["@jobschan"])
    reader = web.WebReader(http_get=lambda url: FakeResp(SAMPLE_HTML))

    ids = web.ingest(cfg, conn, reader=reader)
    assert len(ids) == 2
    assert len(store.list_by_state(conn, "discovered")) == 2

    # Re-running ingests nothing new (dedup on (channel, message_id)).
    ids2 = web.ingest(cfg, conn, reader=reader)
    assert ids2 == []
    assert len(store.list_by_state(conn, "discovered")) == 2


def test_ingest_continues_past_failing_channel(conn):
    calls = {"n": 0}

    def flaky_get(url):
        calls["n"] += 1
        if "bad" in url:
            raise RuntimeError("boom")
        return FakeResp(SAMPLE_HTML)

    cfg = Config(telegram_channels=["@bad", "@jobschan"])
    reader = web.WebReader(http_get=flaky_get)
    ids = web.ingest(cfg, conn, reader=reader)
    # First channel failed; second still ingested its two messages.
    assert len(ids) == 2


def test_ingest_requires_channels(conn):
    cfg = Config(telegram_channels=[])
    reader = web.WebReader(http_get=lambda url: FakeResp(SAMPLE_HTML))
    try:
        web.ingest(cfg, conn, reader=reader)
        assert False, "expected RuntimeError for missing channels"
    except RuntimeError as exc:
        assert "telegram_channels" in str(exc)


# --------------------------------------------------------------------------
# Config loading without Telegram auth (web mode is auth-free)
# --------------------------------------------------------------------------

def test_config_loads_without_telegram_auth():
    """Web mode must work when api_id / api_hash / session are absent.
    load_config with only TELEGRAM_CHANNELS set must produce a valid Config
    with ingest_mode='web' and None auth fields."""
    from job_hunter.config import load_config
    cfg = load_config(env={
        "TELEGRAM_CHANNELS": "@jobschan,@python_jobs",
        "INGEST_MODE": "web",
    })
    assert cfg.ingest_mode == "web"
    assert cfg.telegram_api_id is None
    assert cfg.telegram_api_hash is None
    assert cfg.telegram_session is None
    assert cfg.telegram_channels == ["@jobschan", "@python_jobs"]


def test_config_ingest_mode_defaults_to_web():
    """When INGEST_MODE is not set, the default must be 'web'."""
    from job_hunter.config import load_config
    cfg = load_config(env={"TELEGRAM_CHANNELS": "@c"})
    assert cfg.ingest_mode == "web"


def test_config_ingest_mode_telethon_accepted():
    """INGEST_MODE=telethon must be accepted (case-insensitive)."""
    from job_hunter.config import load_config
    cfg = load_config(env={
        "TELEGRAM_CHANNELS": "@c",
        "INGEST_MODE": "TELETHON",
    })
    assert cfg.ingest_mode == "telethon"


# --------------------------------------------------------------------------
# HTML parser edge cases
# --------------------------------------------------------------------------

def test_parse_channel_html_self_closing_br():
    """A self-closing <br/> inside the text div must become a newline."""
    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="ch/200">'
        '<div class="tgme_widget_message_text js-message_text">'
        'Line one<br/>Line two'
        '</div></div>'
    )
    msgs = web.parse_channel_html(html, "@ch")
    assert len(msgs) == 1
    assert "Line one" in msgs[0].raw_text
    assert "Line two" in msgs[0].raw_text


def test_parse_channel_html_html_entities_decoded():
    """HTML entities like &amp; and &lt; must be decoded in the message text."""
    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="ch/201">'
        '<div class="tgme_widget_message_text js-message_text">'
        'Salary &gt; 200k &amp; remote'
        '</div></div>'
    )
    msgs = web.parse_channel_html(html, "@ch")
    assert len(msgs) == 1
    assert "&amp;" not in msgs[0].raw_text
    assert ">" in msgs[0].raw_text or "&gt;" not in msgs[0].raw_text
    assert "&" in msgs[0].raw_text


def test_parse_channel_html_channel_name_normalization():
    """@-prefixed channel name must be used as-is for source_channel."""
    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="ch/300">'
        '<div class="tgme_widget_message_text js-message_text">'
        'Some job post'
        '</div></div>'
    )
    msgs = web.parse_channel_html(html, "@ch")
    assert msgs[0].source_channel == "@ch"
    # Permalink must strip the @ from the URL
    assert msgs[0].source_link == "https://t.me/ch/300"


def test_ingest_returns_empty_on_http_error(conn):
    """When the HTTP call fails (non-2xx), the channel is skipped gracefully."""
    def error_get(url):
        return FakeResp("", status_ok=False)

    cfg = Config(telegram_channels=["@ch"])
    reader = web.WebReader(http_get=error_get)
    ids = web.ingest(cfg, conn, reader=reader)
    assert ids == []


# --------------------------------------------------------------------------
# Hidden hyperlink TARGET (href) capture into raw_text
# --------------------------------------------------------------------------

from job_hunter import research_fetch as rf


class _Extracted:
    """Stand-in for the extracted record with no link contact set."""
    contact = None
    contact_type = None


def test_apply_href_captured_and_chrome_dropped_end_to_end():
    """The real ai_rabota/1137 case: <a href=career.avito.com>Откликнуться</a>
    plus в VK (vk.com), в Max (max.ru), a t.me link and a ?q=#hashtag link.

    raw_text must CONTAIN the avito apply URL and NOT the vk/max/t.me/?q= ones,
    and the unchanged select_primary_url must then return the avito URL."""
    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="ai_rabota/1137">'
        '<div class="tgme_widget_message_text js-message_text" dir="auto">'
        'Data Scientist в Avito<br>'
        '<a href="https://career.avito.com/vacancies/data-science/19566/">Откликнуться</a> '
        '<a href="https://t.me/ai_rabota/1137">в Telegram</a> '
        '<a href="https://vk.com/ai_rabota">в VK</a> '
        '<a href="https://max.ru/ai_rabota">в Max</a> '
        '<a href="?q=%23senior">#senior</a>'
        '</div>'
        '<a class="tgme_widget_message_date" href="https://t.me/ai_rabota/1137">'
        '<time datetime="2026-06-20T10:00:00+00:00">10:00</time></a>'
        '</div>'
    )
    msgs = web.parse_channel_html(html, "@ai_rabota")
    assert len(msgs) == 1
    raw = msgs[0].raw_text

    assert "https://career.avito.com/vacancies/data-science/19566/" in raw
    # Chrome / mirror / telegram / relative links are NOT appended.
    assert "vk.com" not in raw
    assert "max.ru" not in raw
    assert "t.me" not in raw
    assert "?q=" not in raw
    # Visible body text is preserved.
    assert "Data Scientist в Avito" in raw
    assert "Откликнуться" in raw

    # End-to-end: ingest -> EXISTING selector picks the apply URL.
    picked = rf.select_primary_url(_Extracted(), raw)
    assert picked == "https://career.avito.com/vacancies/data-science/19566/"


def test_visible_text_url_anchor_not_double_printed():
    """ai_rabota/1136 case: the visible anchor text IS the URL (goldapple).
    The link still lands in raw_text and is selector-reachable, without an
    ugly 'url: url' duplication."""
    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="ai_rabota/1136">'
        '<div class="tgme_widget_message_text js-message_text">'
        'Frontend at Gold Apple<br>'
        '<a href="https://job.goldapple.ru/vacancy/12345">https://job.goldapple.ru/vacancy/12345</a>'
        '</div></div>'
    )
    msgs = web.parse_channel_html(html, "@ai_rabota")
    raw = msgs[0].raw_text
    assert "https://job.goldapple.ru/vacancy/12345" in raw
    assert "https://job.goldapple.ru/vacancy/12345: https://job.goldapple.ru/vacancy/12345" not in raw
    assert rf.select_primary_url(_Extracted(), raw) == "https://job.goldapple.ru/vacancy/12345"


def test_repeated_anchor_deduped_once():
    """The remote_ai_jobs getonbrd case: the same href repeated across several
    👉/👈 anchors must appear exactly ONCE in raw_text (deduped by URL)."""
    url = "https://www.getonbrd.com/empleos/ml-engineer-acme"
    anchors = "".join(
        f'<a href="{url}">( Job Description &amp; Apply )</a> ' for _ in range(5)
    )
    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="remote_ai_jobs/55">'
        '<div class="tgme_widget_message_text js-message_text">'
        'ML Engineer at Acme<br>' + anchors +
        '</div></div>'
    )
    msgs = web.parse_channel_html(html, "@remote_ai_jobs")
    raw = msgs[0].raw_text
    assert raw.count(url) == 1
    assert rf.select_primary_url(_Extracted(), raw) == url


def test_garbled_email_as_link_not_appended():
    """The garbled 'http://Контакты:job@selecty.ru/' email-as-link has an '@'
    in the netloc and must NOT be appended."""
    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="ch/77">'
        '<div class="tgme_widget_message_text js-message_text">'
        'HR contact<br>'
        '<a href="http://Контакты:job@selecty.ru/">Контакты: job@selecty.ru</a>'
        '</div></div>'
    )
    msgs = web.parse_channel_html(html, "@ch")
    raw = msgs[0].raw_text
    # Visible anchor text stays; the LINK TARGET (an http URL) is NOT appended.
    assert raw == "HR contact\nКонтакты: job@selecty.ru"
    assert "http://" not in raw
    # No real apply URL -> selector returns None -> desk fallback.
    assert rf.select_primary_url(_Extracted(), raw) is None


def test_no_link_post_is_byte_identical():
    """A post with NO keepable links must yield raw_text byte-identical to the
    pre-change (visible-text-only) output: no trailing separator, no section."""
    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="ch/88">'
        '<div class="tgme_widget_message_text js-message_text">'
        'Plain job post.<br>Salary 200000 RUB.<br>No links here.'
        '</div></div>'
    )
    msgs = web.parse_channel_html(html, "@ch")
    raw = msgs[0].raw_text
    # This is exactly what _clean_text(flattened visible text) produces today.
    assert raw == "Plain job post.\nSalary 200000 RUB.\nNo links here."


def test_chrome_only_post_no_url_appended_falls_back():
    """A post whose ONLY links are chrome (t.me/vk/max) must append nothing and
    the selector must return None (-> desk_fallback, the correct behavior)."""
    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="ch/99">'
        '<div class="tgme_widget_message_text js-message_text">'
        'Generic announcement.<br>'
        '<a href="https://t.me/somechan">в Telegram</a> '
        '<a href="https://vk.com/somechan">в VK</a> '
        '<a href="https://max.ru/somechan">в Max</a>'
        '</div></div>'
    )
    msgs = web.parse_channel_html(html, "@ch")
    raw = msgs[0].raw_text
    # Inline space-separated anchors stay on one visible line; NO link section
    # is appended (all targets are chrome).
    assert raw == "Generic announcement.\nв Telegram в VK в Max"
    assert "t.me" not in raw and "vk.com" not in raw and "max.ru" not in raw
    assert rf.select_primary_url(_Extracted(), raw) is None


def test_multiple_real_links_document_order_preserved():
    """When a post has several keepable links, document order is preserved so
    the FIRST kept link is the real apply URL the selector returns."""
    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="ch/100">'
        '<div class="tgme_widget_message_text js-message_text">'
        'Backend role.<br>'
        '<a href="https://hh.ru/vacancy/111">Откликнуться на hh</a> '
        '<a href="https://acme-corp.com/careers/42">Сайт компании</a> '
        '<a href="https://vk.com/acme">в VK</a>'
        '</div></div>'
    )
    msgs = web.parse_channel_html(html, "@ch")
    raw = msgs[0].raw_text
    hh_pos = raw.index("https://hh.ru/vacancy/111")
    acme_pos = raw.index("https://acme-corp.com/careers/42")
    assert hh_pos < acme_pos  # document order preserved
    assert "vk.com" not in raw
    assert rf.select_primary_url(_Extracted(), raw) == "https://hh.ru/vacancy/111"


def test_select_primary_url_prefers_link_contact_unchanged():
    """select_primary_url is UNCHANGED: an extracted contact of type 'link'
    still wins over the in-body URL once the href is present in raw_text."""
    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="ch/101">'
        '<div class="tgme_widget_message_text js-message_text">'
        'Role.<br><a href="https://career.avito.com/v/1">Откликнуться</a>'
        '</div></div>'
    )
    raw = web.parse_channel_html(html, "@ch")[0].raw_text

    class WithLink:
        contact = "https://apply.example.com/form"
        contact_type = "link"

    assert rf.select_primary_url(WithLink(), raw) == "https://apply.example.com/form"


# --------------------------------------------------------------------------
# Tester-added tests: gaps from adversarial validation pass
# --------------------------------------------------------------------------

# --- Point 5 (extended): non-http schemes and structural rejects dropped ----

import pytest


@pytest.mark.parametrize("href,description", [
    ("mailto:hr@co.com", "mailto: scheme dropped"),
    ("tg://resolve?domain=somebot", "tg:// (Telegram custom scheme) dropped"),
    ("javascript:void(0)", "javascript: scheme dropped"),
    ("//evil.com/path", "scheme-relative URL dropped (no scheme)"),
    ("http://localhost/internal", "localhost has no dot -> dropped"),
    ("http://localhost:8080/x", "localhost with port -> dropped"),
])
def test_should_keep_href_non_http_schemes_and_no_dot_dropped(href, description):
    """_should_keep_href must drop non-http(s) schemes, scheme-relative URLs, and
    hosts without a dot (localhost). None of these are valid apply targets."""
    result = web._should_keep_href(href)
    assert result is False, (
        f"_should_keep_href({href!r}) returned True but should be False: {description}"
    )


def test_non_http_scheme_in_anchor_not_appended_no_crash():
    """A post containing mailto/tg/javascript anchors must not append them and must
    not raise. The only URL that appears must be the one real apply URL."""
    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="ch/200">'
        '<div class="tgme_widget_message_text">'
        'Contact us.<br>'
        '<a href="mailto:hr@company.com">Email HR</a> '
        '<a href="tg://resolve?domain=hrbot">Telegram bot</a> '
        '<a href="javascript:void(0)">JS link</a> '
        '<a href="//evil.com/path">Scheme-relative</a> '
        '<a href="http://localhost/">Localhost</a> '
        '<a href="https://apply.real-company.com/v/1">Apply</a>'
        '</div></div>'
    )
    msgs = web.parse_channel_html(html, "@ch")
    assert len(msgs) == 1
    raw = msgs[0].raw_text
    # Non-http links must NOT appear as captured URLs.
    assert "mailto:" not in raw
    assert "tg://" not in raw
    assert "javascript:" not in raw
    assert "//evil.com" not in raw
    assert "localhost" not in raw
    # The real apply URL must be captured.
    assert "https://apply.real-company.com/v/1" in raw
    # Selector picks the real URL.
    assert rf.select_primary_url(_Extracted(), raw) == "https://apply.real-company.com/v/1"


# --- Point 6: hh.ru and github.com are explicitly KEPT -----------------------

def test_hh_ru_href_kept_and_selector_picks_it():
    """hh.ru must be kept by _should_keep_href (it is NOT in the chrome list).
    This was only in research_fetch's company-anchor skip-list, not here."""
    assert web._should_keep_href("https://hh.ru/vacancy/12345") is True

    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="ch/201">'
        '<div class="tgme_widget_message_text">'
        'Python role.<br>'
        '<a href="https://hh.ru/vacancy/12345">Откликнуться на hh.ru</a>'
        '</div></div>'
    )
    msgs = web.parse_channel_html(html, "@ch")
    raw = msgs[0].raw_text
    assert "https://hh.ru/vacancy/12345" in raw
    assert rf.select_primary_url(_Extracted(), raw) == "https://hh.ru/vacancy/12345"


def test_github_com_href_kept_and_selector_picks_it():
    """github.com must be kept by _should_keep_href (it is NOT in the chrome list).
    Only research_fetch's company-anchor heuristic skips github -- the capture
    keep-list does not."""
    assert web._should_keep_href("https://github.com/company/jobs") is True

    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="ch/202">'
        '<div class="tgme_widget_message_text">'
        'Open source role.<br>'
        '<a href="https://github.com/company/jobs">Apply on GitHub</a>'
        '</div></div>'
    )
    msgs = web.parse_channel_html(html, "@ch")
    raw = msgs[0].raw_text
    assert "https://github.com/company/jobs" in raw
    assert rf.select_primary_url(_Extracted(), raw) == "https://github.com/company/jobs"


# --- Point 7: telegram/chrome exact-suffix semantics + *.t.me.evil.com concern ---

@pytest.mark.parametrize("host,should_keep,description", [
    # Lookalike domains: NOT wrongly excluded (suffix check, not substring)
    ("start.me", True, "start.me is a real service, NOT a t.me subdomain -> kept"),
    ("nott.me", True, "nott.me ends with t.me in chars but no dot separator -> kept"),
    # Exact t.me / subdomains: excluded
    ("t.me", False, "exact t.me -> excluded"),
    ("www.t.me", False, "www.t.me -> telegram subdomain -> excluded"),
    # Attacker-controlled domain embedding 't.me' as a label
    ("t.me.evil.com", True, "t.me.evil.com is NOT a subdomain of t.me -> KEPT (see note)"),
    # Chrome hosts
    ("vk.com", False, "vk.com exact -> chrome host -> excluded"),
    ("evil.vk.com", False, "evil.vk.com -> vk.com subdomain -> excluded"),
    ("vk.com.evil.com", True, "vk.com.evil.com is NOT a subdomain of vk.com -> KEPT"),
])
def test_should_keep_href_exact_suffix_semantics(host, should_keep, description):
    """The keep/drop rule uses exact-or-subdomain matching for both the telegram
    and chrome lists. This test documents the exact semantics including the
    t.me.evil.com / vk.com.evil.com edge cases.

    Note on t.me.evil.com and vk.com.evil.com: they ARE captured because they
    are not subdomains of the excluded hosts. This is the correct behavior for
    the KEEP filter (avoiding false exclusions). Security is handled by the
    downstream SSRF guard in research_fetch._url_is_safe, which validates the
    resolved IP before any fetch.
    """
    href = f"https://{host}/path"
    result = web._should_keep_href(href)
    assert result is should_keep, (
        f"_should_keep_href({href!r}): expected {should_keep}, got {result}. {description}"
    )


# --- Point 9: SSRF guard still blocks a captured URL that resolves to a private IP ---

def test_ssrf_guard_blocks_captured_href_resolving_to_private_ip(monkeypatch):
    """After href capture, select_primary_url picks the URL, but fetch_research_context
    must still block it if the host resolves to a private IP (SSRF guard).
    The href capture path does NOT bypass the SSRF guard.
    """
    from job_hunter import research_fetch as _rf

    # Simulate DNS resolving the apply host to an internal RFC-1918 address.
    monkeypatch.setattr(
        _rf, "_resolve_ips",
        lambda host: ["10.0.0.5"] if "evil-apply" in host else ["93.184.216.34"],
    )

    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="ch/300">'
        '<div class="tgme_widget_message_text">'
        'Apply here.<br>'
        '<a href="https://evil-apply.internal.example.com/v/1">Apply</a>'
        '</div></div>'
    )
    msgs = web.parse_channel_html(html, "@ch")
    raw = msgs[0].raw_text

    # The URL was captured into raw_text.
    assert "https://evil-apply.internal.example.com/v/1" in raw

    # select_primary_url picks it (it passed the ingest-time filter).
    picked = _rf.select_primary_url(_Extracted(), raw)
    assert picked == "https://evil-apply.internal.example.com/v/1"

    # fetch_research_context MUST block it (resolves to private IP).
    called = []

    def fake_get(u, *, timeout, max_redirects):
        called.append(u)
        raise AssertionError("should not have been called")

    result = _rf.fetch_research_context(picked, "Acme", get=fake_get)
    assert result == {"pages": [], "urls": []}, (
        "SSRF guard must block a captured URL that resolves to a private IP"
    )
    assert called == [], "HTTP GET must not have been attempted for a private-IP URL"
