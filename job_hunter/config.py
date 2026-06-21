"""Configuration loaded from environment variables (.env via python-dotenv).

No secret is hardcoded. ``load_config()`` reads the process environment; call
``load_dotenv()`` first in entrypoints to populate it from a .env file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Set


def _split_csv(value: str) -> List[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


def _split_int_set(value: str) -> Set[int]:
    """Parse a comma-separated list of ints into a set, ignoring blanks and
    non-integer tokens. PURE."""
    out: Set[int] = set()
    for tok in value.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(int(tok))
        except ValueError:
            continue
    return out


@dataclass
class Config:
    # Anthropic / LLM
    anthropic_api_key: Optional[str] = None
    # Model routing:
    #   - cheap_model: bulk/cheap steps (extraction). Default Haiku.
    #   - judge_model: judgment/scoring step (relevance). Sonnet.
    # ``anthropic_model`` is kept as a back-compat alias = cheap_model so any
    # existing caller (AnthropicClient default) keeps working.
    cheap_model: str = "claude-haiku-4-5"
    judge_model: str = "claude-sonnet-4-6"
    anthropic_model: str = "claude-haiku-4-5"

    # Ingestion mode: "web" (default, public t.me/s/ over HTTP, NO auth) or
    # "telethon" (optional userbot fallback, requires api_id/hash/session).
    ingest_mode: str = "web"

    # Telegram (Telethon userbot) -- OPTIONAL, only for the "telethon" fallback.
    telegram_api_id: Optional[int] = None
    telegram_api_hash: Optional[str] = None
    telegram_session: Optional[str] = None
    telegram_session_name: str = "job_hunter"
    telegram_channels: List[str] = field(default_factory=list)
    telegram_fetch_limit: int = 50

    # aiogram bot
    bot_token: Optional[str] = None
    notify_chat_id: Optional[int] = None
    # Access control allowlist: Telegram user ids permitted to interact with the
    # bot. When unset, falls back to {notify_chat_id}. When that is also unset,
    # the set is empty and the bot FAILS CLOSED (ignores everyone).
    allowed_user_ids: Set[int] = field(default_factory=set)

    # Storage
    db_path: str = "job_hunter.db"

    # FX
    fx_provider: str = "frankfurter"
    fx_cache_ttl: int = 86400

    # Ingestion date window: for a NEW channel with no watermark, ingest posts
    # from the last ``new_channel_lookback_days`` days. After that, only posts
    # newer than the persisted per-channel watermark are ingested.
    new_channel_lookback_days: int = 14

    def require(self, *names: str) -> None:
        """Raise if any named config attribute is missing/empty."""
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise RuntimeError(
                "Missing required config (set in .env): " + ", ".join(missing)
            )


def _int_or_none(value: str) -> Optional[int]:
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def load_config(env: Optional[dict] = None) -> Config:
    """Build a Config from the given mapping (defaults to os.environ)."""
    e = os.environ if env is None else env

    def get(key: str, default: str = "") -> str:
        return (e.get(key) or default).strip()

    notify_chat_id = _int_or_none(get("NOTIFY_CHAT_ID"))

    # Access allowlist: explicit ALLOWED_USER_IDS wins; otherwise fall back to
    # {notify_chat_id} if set; otherwise an empty set => fail closed.
    allowed_user_ids = _split_int_set(get("ALLOWED_USER_IDS"))
    if not allowed_user_ids and notify_chat_id is not None:
        allowed_user_ids = {notify_chat_id}

    cheap_model = get("ANTHROPIC_CHEAP_MODEL") or get("ANTHROPIC_MODEL") or "claude-haiku-4-5"
    judge_model = get("ANTHROPIC_JUDGE_MODEL") or "claude-sonnet-4-6"

    return Config(
        anthropic_api_key=get("ANTHROPIC_API_KEY") or None,
        cheap_model=cheap_model,
        judge_model=judge_model,
        anthropic_model=cheap_model,
        ingest_mode=(get("INGEST_MODE") or "web").lower(),
        telegram_api_id=_int_or_none(get("TELEGRAM_API_ID")),
        telegram_api_hash=get("TELEGRAM_API_HASH") or None,
        telegram_session=get("TELEGRAM_SESSION") or None,
        telegram_session_name=get("TELEGRAM_SESSION_NAME") or "job_hunter",
        telegram_channels=_split_csv(get("TELEGRAM_CHANNELS")),
        telegram_fetch_limit=_int_or_none(get("TELEGRAM_FETCH_LIMIT")) or 50,
        bot_token=get("BOT_TOKEN") or None,
        notify_chat_id=notify_chat_id,
        allowed_user_ids=allowed_user_ids,
        db_path=get("DB_PATH") or "job_hunter.db",
        fx_provider=get("FX_PROVIDER") or "frankfurter",
        fx_cache_ttl=_int_or_none(get("FX_CACHE_TTL")) or 86400,
        new_channel_lookback_days=_int_or_none(get("NEW_CHANNEL_LOOKBACK_DAYS")) or 14,
    )
