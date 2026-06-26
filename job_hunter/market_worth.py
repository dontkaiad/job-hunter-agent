"""Market Worth — web-grounded salary benchmark for the candidate.

Fetches current salary data via a web-search-enabled model, validates the
result, caches it locally (JSON file), and serves it to the bot (/worth) and
the dashboard (/market-worth).

Entry point: ``get_or_refresh(cfg, profile, force=False, _caller=None)``

``_caller`` is a test seam: pass a ``callable(prompt) -> str`` to bypass the
real Anthropic web_search call in unit tests.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, replace
from typing import List, Optional


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class MarketWorthResult:
    """Structured salary benchmark, validated and cache-ready."""

    ru_min: Optional[int]           # ₽ / month lower bound
    ru_max: Optional[int]           # ₽ / month upper bound
    ru_currency: str                # always "RUB"
    intl_min: Optional[int]         # € or $ / month lower bound
    intl_max: Optional[int]         # € or $ / month upper bound
    intl_currency: str              # "EUR" or "USD"
    sources: List[str]              # URLs or "Site: title" strings
    reasoning_short: str            # 2-3 sentence summary
    computed_at: str                # ISO-8601 timestamp
    degraded: bool = False
    degraded_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Sanity-check bounds (overridable via Config)
# ---------------------------------------------------------------------------

DEFAULT_RU_MIN_FLOOR    = 50_000
DEFAULT_RU_MAX_CEILING  = 700_000
DEFAULT_INTL_MIN_FLOOR  = 1_000
DEFAULT_INTL_MAX_CEILING = 15_000


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def _build_prompt(profile) -> str:
    """Render the web-search prompt from the candidate profile."""
    def _bullets(items):
        if not items:
            return "  - (unspecified)"
        return "\n".join(f"  - {x}" for x in items)

    return (
        "You are a salary research assistant. Use web search to find CURRENT (2026) "
        "salary benchmarks for the candidate profile below.\n\n"
        "CANDIDATE PROFILE:\n"
        f"- Role: {profile.role}\n"
        f"- Grade: {profile.target_grade}\n"
        "- Hands-on skills:\n"
        f"{_bullets(profile.hands_on)}\n"
        "- Tech stack:\n"
        f"{_bullets(profile.stack)}\n"
        f"- Work format preference: {', '.join(profile.location_priority) if profile.location_priority else 'remote'}\n\n"
        "TASK: Search for salary data for this EXACT profile (AI / Prompt / LLM Engineer, "
        "middle grade, skills above). Use at least 2-3 different sources.\n\n"
        "Report SEPARATELY:\n"
        "1. Russia market (₽/month) — hh.ru, habr.com/jobs, zarplata.ru\n"
        "2. International remote (€/month or $/month) — levels.fyi, glassdoor, LinkedIn, "
        "remote.com, European job boards\n\n"
        "Be HONEST and ACCURATE — the candidate will make career decisions from this. "
        "Report the MEDIAN or TYPICAL RANGE, not best-case outliers. Do not inflate or deflate.\n\n"
        "Write a clear research summary: cite the actual sources you found, give the "
        "salary ranges for both markets, and explain the key factors. Plain prose is fine."
    )


# ---------------------------------------------------------------------------
# Web-search call (Anthropic tool_use)
# ---------------------------------------------------------------------------


_EXTRACTION_PROMPT = (
    "Extract salary data from the research text below into a JSON object.\n"
    "Return ONLY valid JSON — no markdown fences, no prose, no explanation.\n\n"
    "Required schema (all salary values are integers, per month):\n"
    '{"ru_min": <int>, "ru_max": <int>, "ru_currency": "RUB",\n'
    ' "intl_min": <int>, "intl_max": <int>, "intl_currency": "EUR" or "USD",\n'
    ' "sources": ["<URL or site name>", ...],\n'
    ' "reasoning_short": "<2-3 sentence summary>"}\n\n'
    "Research text:\n"
)


def _call_with_web_search(
    api_key: str,
    model: str,
    prompt: str,
    *,
    _caller=None,
) -> str:
    """Two-step call: web-grounded research → JSON extraction.

    Step 1 (model + web_search_20250305): search the web and produce prose with
    citations. Asking for JSON here is futile — web_search mode makes the model
    write narrative answers with inline citations and ignore format instructions.

    Step 2 (claude-haiku-4-5, no tools): structure the prose into the required
    JSON schema. No web access needed; the grounded data is already in the prose.

    ``_caller(prompt) -> str`` is a test seam that bypasses both API calls.
    """
    if _caller is not None:
        return _caller(prompt)

    import anthropic as _anthropic

    client = _anthropic.Anthropic(api_key=api_key)

    # --- step 1: grounded research ---
    research_resp = client.messages.create(
        model=model,
        max_tokens=3000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    research_text = next(
        (b.text for b in reversed(research_resp.content)
         if getattr(b, "type", None) == "text"),
        None,
    )
    if not research_text:
        raise ValueError("web_search response contained no text block")

    # --- step 2: JSON extraction (cheap, deterministic) ---
    extraction_resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": _EXTRACTION_PROMPT + research_text}],
    )
    json_text = next(
        (b.text for b in reversed(extraction_resp.content)
         if getattr(b, "type", None) == "text"),
        None,
    )
    if not json_text:
        raise ValueError("JSON extraction response contained no text block")
    return json_text


# ---------------------------------------------------------------------------
# Parse & validate
# ---------------------------------------------------------------------------


def _extract_json(raw: str) -> dict:
    """Extract the first JSON object from raw model text."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError("no JSON object in model response")


def _validate(result: MarketWorthResult, cfg) -> MarketWorthResult:
    """Return result with degraded=True if any sanity check fails."""
    reasons: list[str] = []

    # sources presence is checked, but authenticity is not verified —
    # relies on web_search grounding (the tool forces real fetches before the
    # model writes). A model that ignores web results and invents citations
    # would still pass this check; the only mitigation is the tool being enabled.
    if not result.sources:
        reasons.append("no sources returned — result may be hallucinated")

    ru_floor   = getattr(cfg, "market_worth_ru_min",   DEFAULT_RU_MIN_FLOOR)
    ru_ceil    = getattr(cfg, "market_worth_ru_max",   DEFAULT_RU_MAX_CEILING)
    intl_floor = getattr(cfg, "market_worth_intl_min", DEFAULT_INTL_MIN_FLOOR)
    intl_ceil  = getattr(cfg, "market_worth_intl_max", DEFAULT_INTL_MAX_CEILING)

    if result.ru_min is not None and result.ru_max is not None:
        if result.ru_min > result.ru_max:
            reasons.append(f"ru_min ({result.ru_min}) > ru_max ({result.ru_max})")
        elif not (ru_floor <= result.ru_min and result.ru_max <= ru_ceil):
            reasons.append(
                f"RU range {result.ru_min}–{result.ru_max} outside bounds "
                f"[{ru_floor}, {ru_ceil}]"
            )

    if result.intl_min is not None and result.intl_max is not None:
        if result.intl_min > result.intl_max:
            reasons.append(f"intl_min ({result.intl_min}) > intl_max ({result.intl_max})")
        elif not (intl_floor <= result.intl_min and result.intl_max <= intl_ceil):
            reasons.append(
                f"intl range {result.intl_min}–{result.intl_max} outside bounds "
                f"[{intl_floor}, {intl_ceil}]"
            )

    if reasons:
        return replace(result, degraded=True, degraded_reason="; ".join(reasons))
    return result


def _parse_and_validate(raw: str, cfg) -> MarketWorthResult:
    """Parse model text → validated MarketWorthResult."""
    from .clock import now_iso

    data = _extract_json(raw)
    result = MarketWorthResult(
        ru_min=data.get("ru_min"),
        ru_max=data.get("ru_max"),
        ru_currency=data.get("ru_currency", "RUB"),
        intl_min=data.get("intl_min"),
        intl_max=data.get("intl_max"),
        intl_currency=data.get("intl_currency", "EUR"),
        sources=list(data.get("sources") or []),
        reasoning_short=str(data.get("reasoning_short") or ""),
        computed_at=now_iso(),
    )
    return _validate(result, cfg)


# ---------------------------------------------------------------------------
# Cache (JSON file)
# ---------------------------------------------------------------------------


def load_cache(path: str) -> Optional[MarketWorthResult]:
    """Load the last cached result; return None on miss or corrupt file."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return MarketWorthResult(**data)
    except (FileNotFoundError, json.JSONDecodeError, TypeError, KeyError):
        return None


def save_cache(result: MarketWorthResult, path: str) -> None:
    """Persist result to disk."""
    import os
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(result), f, ensure_ascii=False, indent=2)


def is_stale(result: MarketWorthResult, max_age_days: int) -> bool:
    """True if result is older than max_age_days."""
    from datetime import datetime, timezone

    try:
        computed = datetime.fromisoformat(result.computed_at)
        if computed.tzinfo is None:
            computed = computed.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - computed).days
        return age >= max_age_days
    except (ValueError, TypeError):
        return True


def age_days(result: MarketWorthResult) -> int:
    """Return how many full days old result is (0 if < 1 day)."""
    from datetime import datetime, timezone

    try:
        computed = datetime.fromisoformat(result.computed_at)
        if computed.tzinfo is None:
            computed = computed.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - computed).days)
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def get_or_refresh(
    cfg,
    profile,
    *,
    force: bool = False,
    _caller=None,
) -> MarketWorthResult:
    """Return a fresh-or-cached MarketWorthResult.

    - Returns cached result if within max_age_days and not forced.
    - Fetches via web_search otherwise.
    - On fetch failure: returns stale cache marked degraded, or re-raises if
      no cache exists.
    """
    cache_path = cfg.market_worth_cache_path
    max_age = cfg.market_worth_cache_days
    cached = load_cache(cache_path)

    if cached is not None and not force and not is_stale(cached, max_age):
        return cached

    try:
        prompt = _build_prompt(profile)
        raw = _call_with_web_search(
            cfg.anthropic_api_key or "",
            cfg.market_model,
            prompt,
            _caller=_caller,
        )
        result = _parse_and_validate(raw, cfg)
        save_cache(result, cache_path)
        _log(
            f"📊 market_worth: RU {result.ru_min}–{result.ru_max} ₽ | "
            f"intl {result.intl_min}–{result.intl_max} {result.intl_currency} | "
            f"degraded={result.degraded}"
        )
        return result
    except Exception as exc:
        _log(f"⚠️ market_worth refresh failed: {exc!r}")
        if cached is not None:
            return replace(
                cached, degraded=True,
                degraded_reason=f"refresh failed: {exc}",
            )
        raise


# ---------------------------------------------------------------------------
# Logging (fire-and-forget from sync context)
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    """Print to stdout (visible in docker logs); best-effort send to tg_logger."""
    print(f"[market_worth] {msg}", flush=True)

    def _bg():
        try:
            import asyncio
            from . import tg_logger
            asyncio.run(tg_logger.send_log(msg))
        except Exception:
            pass

    threading.Thread(target=_bg, daemon=True).start()


# ---------------------------------------------------------------------------
# Formatting helpers (bot / dashboard)
# ---------------------------------------------------------------------------


def fmt_range(mn, mx, currency: str, *, suffix: str = "/мес") -> str:
    """Format a salary range as a human-readable string."""
    if mn is None and mx is None:
        return "—"
    sym = {"RUB": "₽", "EUR": "€", "USD": "$"}.get(currency, currency)
    if mn is not None and mx is not None:
        return f"{mn:,}–{mx:,} {sym}{suffix}".replace(",", " ")
    if mn is not None:
        return f"от {mn:,} {sym}{suffix}".replace(",", " ")
    return f"до {mx:,} {sym}{suffix}".replace(",", " ")
