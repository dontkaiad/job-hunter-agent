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
        "You are a salary research assistant. Search the web to find CURRENT (2025-2026) "
        "salary data for the candidate profile below.\n\n"
        "CANDIDATE PROFILE:\n"
        f"- Role: {profile.role}\n"
        f"- Grade: {profile.target_grade}\n"
        "- Skills: "
        f"{', '.join(profile.hands_on) if profile.hands_on else 'AI/LLM engineering'}\n"
        "- Stack: "
        f"{', '.join(profile.stack) if profile.stack else 'Python'}\n\n"
        "SEARCH BOTH MARKETS SEPARATELY and report each:\n\n"
        "MARKET 1 — Russia (₽/month):\n"
        "  Search: AI инженер зарплата 2025, Prompt Engineer зарплата hh.ru, "
        "LLM инженер оклад, ИИ инженер Хабр Карьера зарплаты.\n"
        "  Sources to try: hh.ru/vacancy statistics, habr.com career survey, "
        "getmatch.ru, zarplata.ru, moikrug.ru.\n"
        "  Give a typical RANGE (lower bound AND upper bound) for this grade.\n\n"
        "MARKET 2 — International remote (€/month or $/month):\n"
        "  Search: AI/Prompt/LLM Engineer remote salary Europe 2025, "
        "middle-level AI engineer compensation international.\n"
        "  Sources to try: levels.fyi, glassdoor, LinkedIn salary insights, "
        "Eurostat IT salaries, EU job boards, remote.com.\n"
        "  Give a typical RANGE (lower bound AND upper bound) for this grade.\n\n"
        "RULES:\n"
        "- For EACH market give BOTH a minimum AND a maximum (e.g. '150 000–280 000 ₽').\n"
        "- If a market has limited data, give a wide honest range rather than leaving it empty.\n"
        "- Mention each source by name or URL when you cite a number.\n"
        "- Be ACCURATE — the candidate will make career decisions from this.\n\n"
        "Write a clear prose summary covering both markets. Cite sources explicitly."
    )


# ---------------------------------------------------------------------------
# Web-search call (Anthropic tool_use)
# ---------------------------------------------------------------------------


def _extract_urls_from_text(text: str) -> list:
    """Pull URLs and known job-board domains from prose. Used to populate
    sources[] reliably without relying on the structuring model to find them."""
    urls = re.findall(r'https?://[^\s\)\]\",<>]+', text)
    domains = re.findall(
        r'\b(?:hh\.ru|habr\.com|glassdoor\.com|linkedin\.com|levels\.fyi'
        r'|getmatch\.ru|zarplata\.ru|moikrug\.ru|remote\.com|salary\.ru'
        r'|weworkremotely\.com|indeed\.com|euremotejobs\.com|jobspresso\.co)\b',
        text, re.IGNORECASE,
    )
    seen: set = set()
    result = []
    for s in urls + domains:
        s = s.rstrip('.,;:)\'"')
        if s and s not in seen:
            seen.add(s)
            result.append(s)
    return result[:12]


def _build_extraction_prompt(research_text: str, detected_sources: list) -> str:
    sources_hint = (
        f"\n\nSOURCES DETECTED IN TEXT (include ALL of these verbatim in sources[]):\n"
        + "\n".join(f"  - {s}" for s in detected_sources)
        if detected_sources else ""
    )
    return (
        "Extract salary benchmark data from the research text below.\n"
        "Return ONLY valid JSON, no markdown fences, no prose.\n\n"
        "EXTRACTION RULES — follow strictly:\n"
        "1. ru_min, ru_max: Russian salary in ₽/month, BOTH required integers.\n"
        "   If text has only one RU value V, set min=round(V*0.70), max=round(V*1.35).\n"
        "   If text has no Russian data at all, estimate for AI/LLM Engineer middle "
        "grade Russia: min=150000, max=280000.\n"
        "2. intl_min, intl_max: international remote salary in €/month (or $/month), "
        "BOTH required integers.\n"
        "   Same rule: if only one value V found, min=round(V*0.75), max=round(V*1.40).\n"
        "   If no intl data: estimate for AI/LLM Engineer remote Europe: "
        "min=3500, max=7000.\n"
        "3. ru_currency: always \"RUB\".\n"
        "4. intl_currency: \"EUR\" if euros mentioned or ambiguous, \"USD\" only if "
        "dollars explicitly stated.\n"
        "5. sources: use the detected sources list provided below. If the list is "
        "empty, also scan the text for any URL (https://...) or site name (e.g. "
        "hh.ru, glassdoor) and add those.\n"
        "6. reasoning_short: 2-3 sentences summarising key findings and caveats.\n"
        f"{sources_hint}\n\n"
        "JSON schema (fill ALL fields):\n"
        '{"ru_min": 150000, "ru_max": 280000, "ru_currency": "RUB", '
        '"intl_min": 4000, "intl_max": 7000, "intl_currency": "EUR", '
        '"sources": ["hh.ru", "glassdoor.com"], '
        '"reasoning_short": "Middle AI engineers in Russia earn..."}\n\n'
        "Research text:\n"
        + research_text
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

    print(f"[market_worth] research_text[:600]={research_text[:600]!r}", flush=True)

    # Extract URLs / domain names from prose directly — more reliable than
    # asking Haiku to find them, since Haiku tends to skip citation mining.
    detected_sources = _extract_urls_from_text(research_text)
    print(f"[market_worth] detected_sources={detected_sources}", flush=True)

    # --- step 2: JSON extraction (cheap, deterministic) ---
    # Assistant prefill ("{") forces Haiku to start with "{" — cannot write
    # "Here is the JSON:" or any other preamble. Prepend the brace back after.
    extraction_prompt = _build_extraction_prompt(research_text, detected_sources)
    extraction_resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1000,
        messages=[
            {"role": "user", "content": extraction_prompt},
            {"role": "assistant", "content": "{"},
        ],
    )
    continuation = next(
        (b.text for b in reversed(extraction_resp.content)
         if getattr(b, "type", None) == "text"),
        None,
    )
    if not continuation:
        raise ValueError("JSON extraction response contained no text block")
    json_text = "{" + continuation
    print(f"[market_worth] json_text[:400]={json_text[:400]!r}", flush=True)
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
