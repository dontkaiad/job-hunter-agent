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
