"""Market Worth — salary benchmark derived from the collected vacancy pipeline.

Aggregates salary data from work_items with relevance_score 25–100 (the
relevant pool). Splits by market: Russian (RUB) vs international (EUR/USD or
Jobicy source). Returns honest null / degraded when the sample is too small.

Entry point: ``compute_from_pipeline(conn, cfg)``

For tests: ``_aggregate_salaries(salary_rows, min_sample)`` is pure (no DB).
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from typing import List, Optional


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class MarketWorthResult:
    """Salary benchmark computed from the pipeline's relevant vacancy pool."""

    ru_min: Optional[int]           # ₽/month P25 across RU vacancies
    ru_max: Optional[int]           # ₽/month P75 across RU vacancies
    ru_currency: str                # always "RUB"
    intl_min: Optional[int]         # €/month or $/month P25
    intl_max: Optional[int]         # €/month or $/month P75
    intl_currency: str              # "EUR" or "USD"
    ru_sample_size: int             # vacancies with RU salary data
    intl_sample_size: int           # vacancies with intl salary data
    min_sample: int                 # threshold to show a range
    total_relevant_vacancies: int   # vacancies with score 25–100
    sources: List[str]              # always ["work_items pipeline"]
    reasoning_short: str            # human-readable summary
    computed_at: str                # ISO-8601 timestamp
    degraded: bool = False
    degraded_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Pure aggregation (testable without DB)
# ---------------------------------------------------------------------------


def _percentile(sorted_vals: list, pct: float) -> Optional[float]:
    """Linear interpolation percentile. pct in [0, 100]."""
    if not sorted_vals:
        return None
    idx = (len(sorted_vals) - 1) * pct / 100
    lo = int(idx)
    hi = lo + 1
    if hi >= len(sorted_vals):
        return sorted_vals[lo]
    return sorted_vals[lo] * (1 - idx + lo) + sorted_vals[hi] * (idx - lo)


def _aggregate_salaries(
    salary_rows: list,
    min_sample: int,
    *,
    total_relevant: int = 0,
) -> MarketWorthResult:
    """Pure function: aggregate salary_rows into a MarketWorthResult.

    salary_rows: list of dicts with keys salary_min, salary_max, currency,
    source_channel. None is allowed for missing bounds.

    RU:   currency == "RUB"
    Intl: currency in ("EUR", "USD") OR source_channel starts with "jobicy"
    """
    from .clock import now_iso

    ru_vals: list = []    # flat list of non-None salary numbers from RU vacancies
    intl_vals: list = []  # flat list from intl vacancies
    intl_currencies: list = []

    for row in salary_rows:
        s_min = row.get("salary_min")
        s_max = row.get("salary_max")
        currency = (row.get("currency") or "").upper().strip()
        source_ch = (row.get("source_channel") or "").lower()

        if not currency:
            continue

        is_ru = currency == "RUB"
        is_intl = currency in ("EUR", "USD") or source_ch.startswith("jobicy")

        nums = [v for v in (s_min, s_max) if v is not None]
        if not nums:
            continue

        if is_ru:
            ru_vals.extend(nums)
        elif is_intl:
            intl_vals.extend(nums)
            intl_currencies.append(currency)

    # Sample sizes = vacancies that contributed at least one value
    ru_sample = sum(
        1 for row in salary_rows
        if (row.get("currency") or "").upper() == "RUB"
        and (row.get("salary_min") is not None or row.get("salary_max") is not None)
    )
    intl_currency = (
        max(set(intl_currencies), key=intl_currencies.count)
        if intl_currencies else "EUR"
    )
    intl_sample = sum(
        1 for row in salary_rows
        if (
            (row.get("currency") or "").upper() in ("EUR", "USD")
            or (row.get("source_channel") or "").lower().startswith("jobicy")
        )
        and (row.get("salary_min") is not None or row.get("salary_max") is not None)
    )

    # Compute ranges (P25–P75 across all salary values in the pool)
    ru_min = ru_max = None
    if ru_sample >= min_sample and ru_vals:
        sv = sorted(ru_vals)
        ru_min = int(round(_percentile(sv, 25)))
        ru_max = int(round(_percentile(sv, 75)))
        if ru_min == ru_max:
            ru_max = None  # single-cluster — show min only

    intl_min = intl_max = None
    if intl_sample >= min_sample and intl_vals:
        sv = sorted(intl_vals)
        intl_min = int(round(_percentile(sv, 25)))
        intl_max = int(round(_percentile(sv, 75)))
        if intl_min == intl_max:
            intl_max = None

    # Build degraded reasons
    reasons: list = []
    if ru_sample < min_sample:
        reasons.append(
            f"Россия: {ru_sample} из {min_sample} вакансий с данными о зарплате"
        )
    if intl_sample < min_sample:
        reasons.append(
            f"Международный: {intl_sample} из {min_sample} вакансий с данными о зарплате"
        )

    degraded = bool(reasons)

    # Summary
    if degraded:
        reasoning = (
            "Данных пока недостаточно для надёжной оценки. "
            + " | ".join(reasons)
            + ". Продолжаем собирать."
        )
    else:
        reasoning = (
            f"По {ru_sample} РФ и {intl_sample} международным вакансиям "
            f"со скором 25–100 (диапазон P25–P75)."
        )

    return MarketWorthResult(
        ru_min=ru_min,
        ru_max=ru_max,
        ru_currency="RUB",
        intl_min=intl_min,
        intl_max=intl_max,
        intl_currency=intl_currency,
        ru_sample_size=ru_sample,
        intl_sample_size=intl_sample,
        min_sample=min_sample,
        total_relevant_vacancies=total_relevant,
        sources=["work_items pipeline"],
        reasoning_short=reasoning,
        computed_at=now_iso(),
        degraded=degraded,
        degraded_reason="; ".join(reasons) if reasons else None,
    )


# ---------------------------------------------------------------------------
# DB computation
# ---------------------------------------------------------------------------


def compute_from_pipeline(conn, cfg) -> MarketWorthResult:
    """Query work_items with score 25–100 and aggregate salary data.

    Pure-SQL aggregation — zero API calls.
    """
    min_sample = getattr(cfg, "market_min_sample", 3)

    rows = conn.execute(
        """
        SELECT extracted_json, relevance_score, source_channel
        FROM work_items
        WHERE relevance_score >= 25
          AND extracted_json IS NOT NULL
        """,
    ).fetchall()

    total_relevant = len(rows)

    salary_rows: list = []
    for row in rows:
        try:
            e = json.loads(row["extracted_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        salary_rows.append({
            "salary_min": e.get("salary_min"),
            "salary_max": e.get("salary_max"),
            "currency": e.get("currency"),
            "source_channel": row["source_channel"] or "",
        })

    return _aggregate_salaries(salary_rows, min_sample, total_relevant=total_relevant)


# ---------------------------------------------------------------------------
# Main entry point (compatibility wrapper — no cache, always fresh from DB)
# ---------------------------------------------------------------------------


def get_or_refresh(conn, cfg) -> MarketWorthResult:
    """Compute a fresh MarketWorthResult from the pipeline DB.

    Pure computation — no logging side-effects. Callers that represent an
    explicit user action (POST /refresh, /worth bot command) can log after
    calling this if needed.
    """
    return compute_from_pipeline(conn, cfg)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
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
        return "нет данных"
    sym = {"RUB": "₽", "EUR": "€", "USD": "$"}.get(currency, currency)
    if mn is not None and mx is not None:
        return f"{mn:,}–{mx:,} {sym}{suffix}".replace(",", " ")
    if mn is not None:
        return f"от {mn:,} {sym}{suffix} (потолок не найден)".replace(",", " ")
    return f"до {mx:,} {sym}{suffix}".replace(",", " ")
