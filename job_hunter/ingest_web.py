"""Public t.me/s/ web reader: PRIMARY ingestion path, NO authentication.

This is the default ingestion. It fetches the public web preview page
``https://t.me/s/<channel>`` over HTTP (no api_id/hash/session, no login) and
parses the message blocks out of the returned HTML.

The HTML parsing is a PURE function (``parse_channel_html``) that takes the raw
HTML string + channel name and returns normalized ``IngestMessage`` objects, so
it is unit-testable without the network. The actual HTTP fetch lives in a thin
separate function (``fetch_channel_html`` / ``WebReader.fetch_channel``) that
calls the pure parser -- mirroring how ``fx.py`` separates pure conversion math
from the httpx fetch.

It reuses the existing normalize/store helpers from ``ingest_telegram``
(``normalize_message`` / ``build_message_link`` / ``store_messages``) and the
same ``store.insert_item`` path so dedup (the (channel, message_id) unique
index) and the §4 schema/source fields stay consistent with the Telethon path.

Run a one-shot web ingest:
    python -m job_hunter.ingest_web
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
import psycopg

from . import store
from .clock import now_utc
from .config import Config, load_config
from .ingest_telegram import IngestMessage, normalize_message, store_messages
from .research_fetch import _is_telegram_host

PUBLIC_BASE = "https://t.me/s/{channel}"

# Channel "chrome" social-mirror / cross-post hosts that appear on EVERY
# t.me/s/ post (the "в VK | в Max" links Telegram injects). These are never the
# vacancy's apply/company target, so a captured anchor pointing at one of them
# is dropped. Telegram permalink hosts are handled separately via the shared
# ``_is_telegram_host`` helper from research_fetch. This list is intentionally
# NARROW (telegram + vk.com + max.ru only) -- it is NOT the broader company-
# anchor skip-list in research_fetch, which also drops hh.ru/github/etc. (those
# ARE valid PRIMARY vacancy hosts we explicitly want to keep here).
_CHROME_HOSTS = ("vk.com", "max.ru")

# Block-level tags whose boundaries become newlines when flattening the
# message text div (Telegram uses <br> and <p>-ish wrappers for line breaks).
_BREAK_TAGS = {"br", "p", "div"}

# Void (self-closing) HTML elements: they get a start tag but never an end tag,
# so they must not affect open-tag depth bookkeeping.
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


def _is_chrome_host(host: Optional[str]) -> bool:
    """True if ``host`` is a channel social-mirror / chrome host. PURE.

    Case-insensitive, matches the exact host or any subdomain of ``vk.com`` /
    ``max.ru`` (e.g. ``m.vk.com``). These are the "в VK | в Max" cross-post
    links Telegram injects on every post and are never the apply target.
    """
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    for chrome in _CHROME_HOSTS:
        if h == chrome or h.endswith("." + chrome):
            return True
    return False


def _should_keep_href(href: Optional[str]) -> bool:
    """True if a captured anchor href is a genuine apply/vacancy URL. PURE.

    KEEP only when ALL hold:
      - absolute http:// or https:// (drops relative ``?q=#...`` hashtag links
        and any non-http scheme such as mailto:/tg:),
      - host is NOT a telegram host (t.me / telegram.org / telegram.me, incl.
        subdomains),
      - host is NOT a channel social-mirror / chrome host (vk.com / max.ru),
      - host looks like a real domain: contains a dot, has NO ``@`` in the
        netloc, and contains no spaces/control chars (this drops the garbled
        ``http://Контакты:job@selecty.ru/`` email-as-link).

    hh.ru / career.* / job.goldapple.ru / getonbrd.com / application-form hosts
    / company sites all pass -- those are exactly what we want. Never raises.
    """
    try:
        raw = (href or "").strip()
        if not raw:
            return False
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https"):
            return False
        netloc = parsed.netloc or ""
        # No userinfo / garbled email-as-link, and no whitespace/control chars
        # anywhere in the authority. ``urlparse`` keeps userinfo in netloc, so
        # an "@" here means a host like ``Контакты:job@selecty.ru``.
        if "@" in netloc:
            return False
        if any(ch.isspace() or ord(ch) < 0x20 for ch in netloc):
            return False
        host = parsed.hostname
        if not host or "." not in host:
            return False
        if _is_telegram_host(host):
            return False
        if _is_chrome_host(host):
            return False
        return True
    except Exception:
        return False


def _channel_path(channel: str) -> str:
    """Normalize a configured channel into its public-username path segment."""
    name = channel.strip().lstrip("@")
    # Accept full t.me links too: https://t.me/foo -> foo
    name = re.sub(r"^https?://t\.me/(s/)?", "", name)
    return name.strip("/")


def public_channel_url(channel: str) -> str:
    """Build the public t.me/s/ URL for a channel. PURE."""
    return PUBLIC_BASE.format(channel=_channel_path(channel))


class _MessageHTMLParser(HTMLParser):
    """Streaming parser that extracts (data-post, text) per message block.

    The t.me/s/ markup nests a ``tgme_widget_message`` wrapper carrying a
    ``data-post="<channel>/<id>"`` attribute, and inside it a
    ``tgme_widget_message_text`` div holding the post text. We track depth so a
    nested wrapper does not confuse the text capture.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # Collected results: list of (data_post, text, datetime_str, links)
        # where links is a list of (href, anchor_text) captured inside the text
        # div, in document order.
        self.messages: List[tuple] = []

        self._in_message = False
        self._message_depth = 0  # tag depth inside the current message wrapper
        self._data_post: Optional[str] = None
        self._datetime: Optional[str] = None  # <time datetime="..."> raw value

        self._in_text = False
        self._text_depth = 0  # tag depth inside the current text div
        self._text_parts: List[str] = []

        # Anchor capture INSIDE the text div: ``_cur_href`` is set on an <a>
        # starttag and cleared on </a>; visible text is accumulated from
        # handle_data so each link carries its anchor text. ``_links`` holds the
        # per-message (href, anchor_text) pairs in document order.
        self._links: List[Tuple[Optional[str], List[str]]] = []
        self._cur_href: Optional[str] = None
        self._cur_anchor_text: List[str] = []

    @staticmethod
    def _classes(attrs) -> set:
        for key, val in attrs:
            if key == "class" and val:
                return set(val.split())
        return set()

    @staticmethod
    def _attr(attrs, name) -> Optional[str]:
        for key, val in attrs:
            if key == name:
                return val
        return None

    def handle_starttag(self, tag, attrs):
        classes = self._classes(attrs)

        if "tgme_widget_message" in classes and not self._in_message:
            self._in_message = True
            self._message_depth = 0
            self._data_post = self._attr(attrs, "data-post")
            self._datetime = None
            return

        if self._in_message:
            is_void = tag in _VOID_TAGS

            # The message datetime lives in <time datetime="..."> inside
            # tgme_widget_message_date. Capture the FIRST one for this message.
            if tag == "time" and self._datetime is None:
                dt = self._attr(attrs, "datetime")
                if dt:
                    self._datetime = dt

            if self._in_text and tag in _BREAK_TAGS:
                self._text_parts.append("\n")

            # Void elements never produce an end tag, so they must not change
            # depth bookkeeping for either the message wrapper or text div.
            if is_void:
                return

            self._message_depth += 1

            if "tgme_widget_message_text" in classes and not self._in_text:
                self._in_text = True
                self._text_depth = 0
                self._text_parts = []
                self._links = []
                self._cur_href = None
                self._cur_anchor_text = []
                return

            if self._in_text:
                self._text_depth += 1
                # Capture the apply/vacancy href off the anchor starttag. The
                # visible text follows in handle_data and is attached on </a>.
                if tag == "a":
                    self._cur_href = self._attr(attrs, "href")
                    self._cur_anchor_text = []

    def handle_startendtag(self, tag, attrs):
        # Self-closing <br/> inside the text div.
        if self._in_message and self._in_text and tag in _BREAK_TAGS:
            self._text_parts.append("\n")

    def handle_data(self, data):
        if self._in_text:
            self._text_parts.append(data)
            if self._cur_href is not None:
                self._cur_anchor_text.append(data)

    def handle_endtag(self, tag):
        if not self._in_message or tag in _VOID_TAGS:
            return

        if self._in_text and tag == "a" and self._cur_href is not None:
            # Close the current anchor: record (href, accumulated anchor text).
            self._links.append((self._cur_href, self._cur_anchor_text))
            self._cur_href = None
            self._cur_anchor_text = []

        if self._in_text:
            if self._text_depth == 0:
                # Closing the text div itself (it still counts toward the
                # message-wrapper depth, so fall through to decrement below).
                self._in_text = False
            else:
                self._text_depth -= 1
                if tag in _BREAK_TAGS:
                    self._text_parts.append("\n")

        if self._message_depth == 0:
            # Closing the message wrapper -> flush this message.
            text = "".join(self._text_parts)
            self.messages.append(
                (self._data_post, text, self._datetime, self._links)
            )
            self._in_message = False
            self._data_post = None
            self._datetime = None
            self._text_parts = []
            self._links = []
            self._cur_href = None
            self._cur_anchor_text = []
        else:
            self._message_depth -= 1


def _clean_text(text: str) -> str:
    """Collapse whitespace from flattened HTML into clean plain text. PURE."""
    text = unescape(text)
    text = text.replace("\xa0", " ")
    lines = [ln.strip() for ln in text.split("\n")]
    # Drop leading/trailing blank lines, collapse runs of blank lines to one.
    out: List[str] = []
    for ln in lines:
        if ln:
            out.append(ln)
        elif out and out[-1] != "":
            out.append("")
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out).strip()


def _append_links(text: str, links: List[Tuple[Optional[str], List[str]]]) -> str:
    """Append kept apply/vacancy hrefs to the cleaned post text. PURE.

    For each captured (href, anchor_text) inside the message text div, keep the
    href only if ``_should_keep_href`` passes (real apply/company URL, not
    telegram/vk/max chrome, not a relative hashtag link, not a garbled email).
    DEDUPE by URL preserving DOCUMENT ORDER, so the first kept link is the real
    apply URL (the getonbrd post repeats the same href several times -> once).

    Each kept link is appended on its own line as ``"<anchor_text>: <url>"``
    (falling back to the bare URL when the anchor had no visible text), so the
    URL becomes reachable by ``research_fetch.select_primary_url`` (which scans
    raw_text for the first non-telegram http(s) URL).

    CRITICAL: when there are NO kept links, the returned text is BYTE-IDENTICAL
    to ``text`` -- no trailing separator, no empty section -- so no-link posts
    are unchanged versus today.
    """
    seen = set()
    lines: List[str] = []
    for href, anchor_parts in links:
        if not _should_keep_href(href):
            continue
        url = unescape((href or "").strip())
        if url in seen:
            continue
        seen.add(url)
        anchor = _clean_text("".join(anchor_parts or []))
        # Avoid a redundant "url: url" when the visible text WAS the URL.
        if anchor and anchor != url:
            lines.append(f"{anchor}: {url}")
        else:
            lines.append(url)

    if not lines:
        return text
    suffix = "\n".join(lines)
    return f"{text}\n{suffix}" if text else suffix


def _message_id_from_data_post(data_post: Optional[str]) -> Optional[int]:
    """Extract the numeric message id from a ``channel/123`` data-post. PURE."""
    if not data_post:
        return None
    tail = data_post.rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def parse_post_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse a t.me/s/ <time datetime="..."> value into a tz-aware UTC datetime.

    PURE. Telegram emits ISO-8601 with an offset (e.g.
    ``2026-06-20T10:00:00+00:00``). Returns an offset-aware UTC datetime, or
    None when the value is missing/unparseable. A naive value (no offset) is
    assumed to be UTC.
    """
    if not value:
        return None
    v = value.strip()
    # Python's fromisoformat handles the trailing 'Z' only on 3.11+; normalize.
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_channel_html(html: str, channel: str) -> List[IngestMessage]:
    """Parse t.me/s/ HTML into normalized IngestMessage objects. PURE.

    Extracts, per message block: the plain message text, the message id (from
    the ``data-post`` attribute), the post datetime (from the
    ``tgme_widget_message_date`` <time datetime=...>), and -- via the shared
    ``normalize_message`` helper -- the t.me/<channel>/<id> permalink.
    Empty/whitespace-only or id-less blocks are skipped gracefully.
    """
    parser = _MessageHTMLParser()
    parser.feed(html or "")
    parser.close()

    out: List[IngestMessage] = []
    for data_post, raw, dt_str, links in parser.messages:
        message_id = _message_id_from_data_post(data_post)
        if message_id is None:
            continue
        text = _clean_text(raw)
        # Append genuine apply/vacancy hrefs that the visible-text-only parse
        # would otherwise drop, so they become selector-reachable. No-link posts
        # are left byte-identical (see ``_append_links``).
        text = _append_links(text, links)
        posted_at = parse_post_datetime(dt_str)
        norm = normalize_message(channel, message_id, text, posted_at=posted_at)
        if norm is not None:
            out.append(norm)
    return out


def fetch_channel_html(channel: str, http_get: Optional[Callable[[str], "httpx.Response"]] = None) -> str:
    """Fetch the raw t.me/s/ HTML for a channel. Thin HTTP I/O wrapper.

    ``http_get`` is injectable for tests; defaults to a real httpx GET. No
    authentication of any kind is performed.
    """
    get = http_get or _default_get
    resp = get(public_channel_url(channel))
    resp.raise_for_status()
    return resp.text


def _default_get(url: str) -> "httpx.Response":
    # A browser-ish UA: t.me/s/ serves the preview HTML reliably this way.
    headers = {"User-Agent": "Mozilla/5.0 (compatible; job-hunter-agent/1.0)"}
    return httpx.get(url, timeout=20.0, follow_redirects=True, headers=headers)


class WebReader:
    """Fetches + parses public channels. HTTP fetch injectable for tests."""

    def __init__(self, http_get: Optional[Callable[[str], "httpx.Response"]] = None) -> None:
        self._http_get = http_get

    def fetch_channel(self, channel: str) -> List[IngestMessage]:
        """Fetch one channel's public page and parse it. Network I/O."""
        html = fetch_channel_html(channel, http_get=self._http_get)
        return parse_channel_html(html, channel)


def ingestion_cutoff(
    watermark: Optional[datetime], now: datetime, lookback_days: int
) -> datetime:
    """Compute the oldest post datetime to ingest. PURE.

    - Existing channel (watermark present): cutoff = the watermark (only posts
      STRICTLY newer than it are ingested).
    - New channel (watermark None): cutoff = now - lookback_days.
    """
    if watermark is not None:
        return watermark
    return now - timedelta(days=lookback_days)


def filter_new_messages(
    messages: List[IngestMessage], cutoff: datetime
) -> List[IngestMessage]:
    """Keep only messages strictly newer than ``cutoff``. PURE.

    Messages with an unknown ``posted_at`` are KEPT (we cannot prove they are
    old; dedup still prevents reprocessing).
    """
    out: List[IngestMessage] = []
    for m in messages:
        if m.posted_at is None or m.posted_at > cutoff:
            out.append(m)
    return out


def ingest(cfg: Config, conn: psycopg.Connection, reader: Optional[WebReader] = None) -> List[int]:
    """Read every configured channel over public HTTP and store new items.

    INCREMENTAL + date-aware:
      - A NEW channel (no persisted watermark) ingests the last
        ``cfg.new_channel_lookback_days`` days.
      - An existing channel ingests only posts newer than its watermark.
      - The per-channel watermark advances to the newest post seen.

    Synchronous (httpx). Reuses ``store_messages`` so dedup + schema match the
    Telethon path. ``reader`` is injectable for tests (mock the HTTP fetch).
    """
    cfg.require("telegram_channels")
    reader = reader or WebReader()
    now = now_utc()
    new_ids: List[int] = []
    for channel in cfg.telegram_channels:
        try:
            msgs = reader.fetch_channel(channel)
        except Exception as exc:  # noqa: BLE001 - keep ingesting other channels
            print(f"[ingest-web] channel {channel} failed: {exc}")
            continue

        watermark = store.get_channel_watermark(conn, channel)
        cutoff = ingestion_cutoff(watermark, now, cfg.new_channel_lookback_days)
        fresh = filter_new_messages(msgs, cutoff)

        ids = store_messages(conn, fresh)
        print(
            f"[ingest-web] {channel}: {len(msgs)} read, {len(fresh)} after date "
            f"filter (cutoff {cutoff.isoformat()}), {len(ids)} new"
        )
        new_ids.extend(ids)

        # Advance the watermark to the newest post seen on the page (use ALL
        # parsed messages, not just the fresh ones, so it tracks the channel
        # head even when nothing new passed the filter).
        dated = [m.posted_at for m in msgs if m.posted_at is not None]
        if dated:
            store.set_channel_watermark(conn, channel, max(dated))

    return new_ids


def ingest_with_own_connection(cfg: Config, reader: Optional[WebReader] = None) -> List[int]:
    """Open a fresh psycopg connection, ingest, and close it -- all in the
    CALLING thread.

    Each thread owns its OWN database connection. ``run.ingest`` offloads the
    synchronous web path via ``asyncio.to_thread``, so the connection it uses
    for the store writes is created inside that worker thread (not shared in
    from the main thread). This function is the unit of work handed to the
    thread: it owns its connection end-to-end and never crosses it back over the
    thread boundary.

    ``reader`` is injectable for tests (mock the HTTP fetch); the storage layer
    is intentionally NOT injectable here so the real thread-own-connection
    behaviour is exercised.
    """
    conn = store.connect(cfg.database_url)
    try:
        store.init_db(conn)
        return ingest(cfg, conn, reader=reader)
    finally:
        conn.close()


def run_ingest() -> List[int]:
    """Synchronous entrypoint: load config, init DB, ingest over public HTTP."""
    from dotenv import load_dotenv

    load_dotenv()
    cfg = load_config()
    return ingest_with_own_connection(cfg)


def main() -> None:
    new_ids = run_ingest()
    print(f"[ingest-web] done: {len(new_ids)} new items")


if __name__ == "__main__":
    main()
