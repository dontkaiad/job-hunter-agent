"""Telethon userbot ingestion: read job posts from Telegram channels.

I/O module. Reads recent messages from configured channels and inserts each as
a work_item via store.insert_item (dedup by (channel, message_id)). After
insert, items can be driven through the deterministic pipeline to the gate.

The message-normalization helpers are pure and unit-tested; the Telethon
network parts are exercised only via the real CLI (require credentials).

Run a one-shot ingest:
    python -m job_hunter.ingest_telegram

Generate a reusable StringSession (interactive login):
    python -m job_hunter.ingest_telegram --login
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from . import digest, store
from .config import Config, load_config


@dataclass
class IngestMessage:
    """A normalized inbound message ready for store.insert_item.

    ``posted_at`` is the original post's timezone-aware UTC datetime (when the
    source provides it, e.g. the t.me/s/ <time datetime=...>). Used for the
    incremental date watermark; None when unknown.
    """

    source_channel: str
    source_message_id: str
    source_link: Optional[str]
    raw_text: str
    posted_at: Optional[datetime] = None


def build_message_link(channel: str, message_id: int) -> Optional[str]:
    """Build a t.me permalink for a public channel. PURE.

    Returns None for non-username channels (private/numeric) where no public
    link exists.
    """
    name = channel.lstrip("@")
    if name and not name.lstrip("-").isdigit():
        return f"https://t.me/{name}/{message_id}"
    return None


def normalize_message(
    channel: str,
    message_id: int,
    text: Optional[str],
    posted_at: Optional[datetime] = None,
) -> Optional[IngestMessage]:
    """Turn a raw Telethon message into an IngestMessage. PURE.

    Returns None for empty/whitespace-only posts (nothing to score).
    """
    if not text or not text.strip():
        return None
    return IngestMessage(
        source_channel=channel,
        source_message_id=str(message_id),
        source_link=build_message_link(channel, message_id),
        raw_text=text.strip(),
        posted_at=posted_at,
    )


def expand_message(msg: IngestMessage) -> List[IngestMessage]:
    """Expand a single message into per-vacancy IngestMessages. PURE.

    - Not a digest -> [msg] unchanged.
    - Digest that splits reliably -> one IngestMessage per vacancy chunk, each
      with a distinct source_message_id ("<id>#<n>") so dedup stays correct and
      the permalink still points to the original message.
    - Digest that CANNOT be split reliably -> [] (caller skips it; never emit a
      single contaminated item).
    """
    if not digest.is_digest(msg.raw_text):
        return [msg]

    chunks = digest.split_digest(msg.raw_text)
    if len(chunks) < 2:
        return []  # detected digest but not splittable -> skip

    out: List[IngestMessage] = []
    for n, chunk in enumerate(chunks, start=1):
        out.append(
            IngestMessage(
                source_channel=msg.source_channel,
                source_message_id=f"{msg.source_message_id}#{n}",
                source_link=msg.source_link,
                raw_text=chunk,
                posted_at=msg.posted_at,
            )
        )
    return out


def store_messages(conn: sqlite3.Connection, messages: List[IngestMessage]) -> List[int]:
    """Insert normalized messages, skipping duplicates. Returns new item ids.

    Digest bundles are expanded into per-vacancy items (or skipped when not
    reliably splittable). Pure-ish: only touches the store. No network.
    """
    new_ids: List[int] = []
    for m in messages:
        expanded = expand_message(m)
        if not expanded:
            print(
                f"[ingest] skipped digest {m.source_channel}/{m.source_message_id}: "
                "not reliably splittable"
            )
            continue
        for piece in expanded:
            item_id = store.insert_item(
                conn,
                raw_text=piece.raw_text,
                source_channel=piece.source_channel,
                source_link=piece.source_link,
                source_message_id=piece.source_message_id,
            )
            if item_id is not None:
                new_ids.append(item_id)
    return new_ids


def _build_client(cfg: Config):
    """Create a Telethon client from config. Imported lazily."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    cfg.require("telegram_api_id", "telegram_api_hash")
    if cfg.telegram_session:
        session = StringSession(cfg.telegram_session)
    else:
        session = cfg.telegram_session_name
    return TelegramClient(session, cfg.telegram_api_id, cfg.telegram_api_hash)


async def fetch_channel_messages(client, channel: str, limit: int) -> List[IngestMessage]:
    """Pull up to ``limit`` recent messages from a channel. Network I/O."""
    out: List[IngestMessage] = []
    async for msg in client.iter_messages(channel, limit=limit):
        # Telethon message.date is timezone-aware UTC.
        posted_at = getattr(msg, "date", None)
        norm = normalize_message(
            channel, msg.id,
            getattr(msg, "message", None) or getattr(msg, "text", None),
            posted_at=posted_at,
        )
        if norm is not None:
            out.append(norm)
    return out


async def ingest_async(cfg: Config, conn: sqlite3.Connection) -> List[int]:
    """Connect, read all configured channels, store new items. Network I/O."""
    cfg.require("telegram_channels")
    client = _build_client(cfg)
    new_ids: List[int] = []
    async with client:
        for channel in cfg.telegram_channels:
            try:
                msgs = await fetch_channel_messages(client, channel, cfg.telegram_fetch_limit)
            except Exception as exc:  # noqa: BLE001 - keep ingesting other channels
                print(f"[ingest] channel {channel} failed: {exc}")
                continue
            ids = store_messages(conn, msgs)
            print(f"[ingest] {channel}: {len(msgs)} read, {len(ids)} new")
            new_ids.extend(ids)
    return new_ids


def run_ingest() -> List[int]:
    """Synchronous entrypoint: load config, init DB, ingest."""
    from dotenv import load_dotenv

    load_dotenv()
    cfg = load_config()
    conn = store.connect(cfg.db_path)
    store.init_db(conn)
    try:
        return asyncio.run(ingest_async(cfg, conn))
    finally:
        conn.close()


async def _login_async(cfg: Config) -> str:
    """Interactive login that prints a reusable StringSession."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    cfg.require("telegram_api_id", "telegram_api_hash")
    client = TelegramClient(StringSession(), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.start()
    session_str = client.session.save()
    await client.disconnect()
    return session_str


def _login() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    cfg = load_config()
    session_str = asyncio.run(_login_async(cfg))
    print("\nYour TELEGRAM_SESSION (put this in .env):\n")
    print(session_str)


def main() -> None:
    import sys

    if "--login" in sys.argv:
        _login()
        return
    new_ids = run_ingest()
    print(f"[ingest] done: {len(new_ids)} new items")


if __name__ == "__main__":
    main()
