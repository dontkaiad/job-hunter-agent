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
    # Confidence corridor: Haiku scores in [score_corridor_lo, score_corridor_hi]
    # trigger a full judge re-score. Defaults match scoring.SCORE_CORRIDOR_LO/HI.
    score_corridor_lo: int = 50
    score_corridor_hi: int = 70

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

    # Storage: PostgreSQL connection DSN (DATABASE_URL), e.g.
    #   postgresql://USER:PASSWORD@HOST:5432/jobhunter
    # Required at runtime (store.connect opens a psycopg3 connection from it).
    database_url: str = ""

    # Ops logging (OPTIONAL): a separate "ops" Telegram bot/chat/thread that
    # receives lifecycle pings (startup) and error notifications. These are
    # first-class config fields, but tg_logger reads os.environ directly so the
    # logger stays self-contained/importable with no Config dependency. When the
    # token or chat id is empty the logger no-ops (graceful degradation).
    tg_log_bot_token: Optional[str] = None
    tg_log_chat_id: Optional[int] = None
    tg_log_thread_jobhunter: Optional[int] = None

    # --- Dashboard auth (issue #5): Telegram-Login-Widget + shared SSO ---
    # Token of the LOGIN bot (the log-bot, e.g. l4rk_sys_bot) that SIGNS the
    # widget; verify_login_widget uses it. Read from env, NEVER hardcoded.
    tg_login_bot_token: Optional[str] = None
    # USERNAME of that same login bot (no @) for the widget's data-telegram-login
    # attribute on the /login page.
    tg_login_bot_username: Optional[str] = None
    # The SAME signing secret across ALL heylark apps — this shared value is what
    # makes the cookie a cross-subdomain SSO token. Required for auth to work.
    session_secret: Optional[str] = None
    # Cookie Domain so the session is shared across *.heylark.dev subdomains.
    cookie_domain: str = ".heylark.dev"
    # Telegram user ids that are ALWAYS authorized for every app (no grant row).
    superuser_tg_ids: Set[int] = field(default_factory=set)
    # DSN of the SHARED auth Postgres holding the grants table. SEPARATE from
    # database_url (the pipeline DB). Required for authorization.
    auth_database_url: str = ""
    # Public https origin of the dashboard (e.g. https://jobs.heylark.dev), used
    # to render the Telegram Login Widget's ABSOLUTE data-auth-url. Required for
    # mobile login (oauth.telegram.org needs an absolute callback). Behind a
    # reverse proxy the request host is unreliable, so this is explicit. Empty =>
    # the /login page falls back to the relative "/auth/callback" (today's
    # behavior; works on desktop / local dev).
    dashboard_public_url: str = ""
    # Public https origin of the standalone login service (login.heylark.dev).
    # Used by login_service to build the absolute widget callback URL. Empty =>
    # relative "/auth/callback" fallback.
    login_public_url: str = ""

    # --- Jobicy international-remote source (OPTIONAL) ---
    # Jobicy (jobicy.com/api/v2/remote-jobs) is a no-auth JSON job board used as
    # the first international-remote auto-source (EU relocation / Blue Card goal).
    # ``jobicy_geos`` is a SMALL list of geo slugs swept each harvest (e.g.
    # europe,poland,serbia,czechia,germany). EMPTY => the source is DISABLED
    # (no Jobicy fetch happens), so it is purely opt-in. Each geo watermarks
    # independently (channel_state key "jobicy:<geo>") while all geos share
    # source_channel="jobicy" so the same job dedups across overlapping sweeps.
    jobicy_geos: List[str] = field(default_factory=list)
    # Industry filter (Jobicy taxonomy). "dev" is the densest AI/ML feed per the
    # recon; tag-filtering is intentionally NOT used (its tag taxonomy is broken).
    jobicy_industry: str = "dev"
    # Results per geo per fetch (Jobicy caps at 50).
    jobicy_count: int = 50

    # --- Harvest quality: minimum score to persist a vacancy in work_items ---
    # Vacancies with relevance_score < min_persist_score are deleted after
    # scoring and never reach work_items in a permanent state. Set to 0 to
    # disable (keep everything). Default: 25.
    min_persist_score: int = 25

    # --- Market Worth: pipeline-derived salary benchmark (zero API) ---
    # Aggregates salary data from work_items with score 25–100. No LLM call.
    # Minimum number of vacancies with salary data per market to show a range.
    # Below this threshold, the result is honest-degraded ("ещё копится").
    market_min_sample: int = 5

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
    score_corridor_lo = int(get("SCORE_CORRIDOR_LO") or 50)
    score_corridor_hi = int(get("SCORE_CORRIDOR_HI") or 70)

    return Config(
        anthropic_api_key=get("ANTHROPIC_API_KEY") or None,
        cheap_model=cheap_model,
        judge_model=judge_model,
        anthropic_model=cheap_model,
        score_corridor_lo=score_corridor_lo,
        score_corridor_hi=score_corridor_hi,
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
        database_url=get("DATABASE_URL"),
        tg_log_bot_token=get("TG_LOG_BOT_TOKEN") or None,
        tg_log_chat_id=_int_or_none(get("TG_LOG_CHAT_ID")),
        tg_log_thread_jobhunter=_int_or_none(get("TG_LOG_THREAD_JOBHUNTER")),
        tg_login_bot_token=get("TG_LOGIN_BOT_TOKEN") or None,
        tg_login_bot_username=get("TG_LOGIN_BOT_USERNAME") or None,
        session_secret=get("SESSION_SECRET") or None,
        cookie_domain=get("COOKIE_DOMAIN") or ".heylark.dev",
        superuser_tg_ids=_split_int_set(get("SUPERUSER_TG_IDS")),
        auth_database_url=get("AUTH_DATABASE_URL"),
        dashboard_public_url=get("DASHBOARD_PUBLIC_URL"),
        login_public_url=get("LOGIN_PUBLIC_URL"),
        min_persist_score=_int_or_none(get("MIN_PERSIST_SCORE")) or 25,
        market_min_sample=_int_or_none(get("MARKET_MIN_SAMPLE")) or 5,
        fx_provider=get("FX_PROVIDER") or "frankfurter",
        fx_cache_ttl=_int_or_none(get("FX_CACHE_TTL")) or 86400,
        new_channel_lookback_days=_int_or_none(get("NEW_CHANNEL_LOOKBACK_DAYS")) or 14,
        jobicy_geos=_split_csv(get("JOBICY_GEOS")),
        jobicy_industry=get("JOBICY_INDUSTRY") or "dev",
        jobicy_count=_int_or_none(get("JOBICY_COUNT")) or 50,
    )
