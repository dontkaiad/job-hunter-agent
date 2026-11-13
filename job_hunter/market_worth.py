"""Market Worth — salary benchmark derived from the collected vacancy pipeline.

Aggregates salary data from work_items with relevance_score 25–100. Splits by market:
Russian (RUB) vs international (everything else, normalised to EUR). Returns honest
null / degraded when the sample is too small.

What is normalised, and how (see ``normalize_monthly``): the extract schema carries only
``salary_min / salary_max / currency`` — no period, no gross/net — so the pay PERIOD is
a HEURISTIC on magnitude (thresholds chosen from the data, constants below); other currencies
are converted to EUR with the FX rates; each vacancy contributes ONE value, the midpoint of its
range; vacancies the pipeline already judged geo-ineligible are left out; rows whose salary came from the heuristic
extractor (``extract_mode`` ``heuristic*``) are left out of both pools (B2). Gross/net is NOT
normalised (nothing reliable in the data to do it with); it is only counted in the diagnostics.

Entry point: ``compute_from_pipeline(conn, cfg)``

For tests: ``_aggregate_salaries(salary_rows, min_sample, rates_per_rub=...)`` is pure (no DB).
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# HEURISTIC thresholds for the pay period (chosen from 523 salaried vacancies, 2026-11-11)
# ---------------------------------------------------------------------------
# EUR-equivalent of the TOP of the range. In the data, USD/EUR/GBP bounds form two clusters with an
# empty valley between them: 1 000-15 000 (monthly: 95 bounds) and 40 000+ (yearly: ~515 bounds),
# only one bound in 15 000-25 000. 20 000 sits in the valley.
INTL_MONTH_MIN_EUR = 1_000       # below: hourly / daily rate or an extraction slip -> dropped
INTL_ANNUAL_MIN_EUR = 20_000     # at/above: a YEARLY figure -> /12
INTL_ANNUAL_MAX_EUR = 1_000_000  # above: implausible -> dropped
# RUB: monthly pay clusters at 20k-700k; from 1 000 000 the figures read as yearly.
RU_MONTH_MIN = 20_000
RU_ANNUAL_MIN = 1_000_000
RU_ANNUAL_MAX = 6_000_000
HEURISTIC_NOTE = (
    "Эвристика: период угадан по порогам (EUR/USD/GBP от ~20 000 EUR-экв. считаем годовыми и делим на 12; "
    "ниже 1 000 и выше 1 000 000 отброшены; РФ: от 1 млн ₽ годовые). Gross/net не нормализован. "
    "Оценка, не измерение."
)
GRADES = ("junior", "middle", "senior", "lead")


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class MarketWorthResult:
    """Salary benchmark computed from the pipeline's relevant vacancy pool."""

    ru_min: Optional[int]           # ₽/month P25 of the per-vacancy midpoints (RU)
    ru_max: Optional[int]           # ₽/month P75
    ru_currency: str                # always "RUB"
    intl_min: Optional[int]         # €/month P25 of the per-vacancy midpoints (intl, converted to EUR)
    intl_max: Optional[int]         # €/month P75
    intl_currency: str              # "EUR" (all intl figures are converted)
    ru_sample_size: int             # vacancies that contributed a monthly RU figure
    intl_sample_size: int           # vacancies that contributed a monthly intl figure
    min_sample: int                 # threshold to show a range
    total_relevant_vacancies: int   # vacancies with score 25–100
    sources: List[str]              # always ["work_items pipeline"]
    reasoning_short: str            # human-readable summary
    computed_at: str                # ISO-8601 timestamp
    degraded: bool = False
    degraded_reason: Optional[str] = None
    ru_median: Optional[int] = None
    intl_median: Optional[int] = None
    # the same three figures for the middle-grade slice (seniority middle / middle+); None when too few
    ru_middle_min: Optional[int] = None
    ru_middle_median: Optional[int] = None
    ru_middle_max: Optional[int] = None
    ru_middle_sample: int = 0
    intl_middle_min: Optional[int] = None
    intl_middle_median: Optional[int] = None
    intl_middle_max: Optional[int] = None
    intl_middle_sample: int = 0
    heuristic_note: str = HEURISTIC_NOTE
    # diagnostics: what the normalisation did (counts of vacancies)
    annual_converted: Dict[str, int] = field(default_factory=dict)   # {"ru": n, "intl": n}
    dropped: Dict[str, int] = field(default_factory=dict)            # reason -> n
    geo_excluded: int = 0           # skipped because the geo guard judged them ineligible
    ru_heuristic_excluded: int = 0    # RU vacancies with a salary left out because the heuristic extractor read it (B2)
    intl_heuristic_excluded: int = 0  # the same for the international pool
    # what the INTERNATIONAL pool is made of (so the page can say what the figure is and is not)
    intl_geo_unchecked: int = 0     # pool vacancies the geo guard never judged (empty verdict): may be country-restricted
    intl_usd_share: Optional[float] = None         # 0..1, share of the pool quoted in USD
    intl_top_source: Optional[str] = None          # the source channel contributing the most pool vacancies
    intl_top_source_share: Optional[float] = None  # 0..1


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


def normalize_monthly(
    salary_min, salary_max, currency, rates_per_rub: Optional[Dict[str, float]] = None
) -> Tuple[Optional[float], str, str]:
    """(monthly midpoint, market, how) for one vacancy; value None when it cannot be used. PURE.

    market is "ru" (RUB) or "intl" (converted to EUR). ``how`` is "monthly" / "annual" on success,
    otherwise the drop reason: no_currency, no_value, no_rate, implausible_low, implausible_high.
    The pay period is the HEURISTIC described in the module header (thresholds above)."""
    nums = [float(v) for v in (salary_min, salary_max) if v is not None]
    cur = (currency or "").upper().strip()
    if not cur:
        return None, "", "no_currency"
    if not nums:
        return None, "", "no_value"
    top, mid = max(nums), sum(nums) / len(nums)
    if cur == "RUB":
        if top < RU_MONTH_MIN:
            return None, "ru", "implausible_low"
        if top <= RU_ANNUAL_MIN:
            return mid, "ru", "monthly"
        if top <= RU_ANNUAL_MAX:
            return mid / 12.0, "ru", "annual"
        return None, "ru", "implausible_high"
    rates = rates_per_rub or {}
    if cur != "EUR" and (not rates.get(cur) or not rates.get("EUR")):
        return None, "intl", "no_rate"
    k = 1.0 if cur == "EUR" else rates[cur] / rates["EUR"]
    top_eur, mid_eur = top * k, mid * k
    if top_eur < INTL_MONTH_MIN_EUR:
        return None, "intl", "implausible_low"
    if top_eur < INTL_ANNUAL_MIN_EUR:
        return mid_eur, "intl", "monthly"
    if top_eur <= INTL_ANNUAL_MAX_EUR:
        return mid_eur / 12.0, "intl", "annual"
    return None, "intl", "implausible_high"


def monthly_top_rub(salary_min, salary_max, currency, convert=None) -> Optional[float]:
    """The TOP of a salary range as a plausible MONTHLY amount in RUB, or None = unknown. PURE.

    The salary hard-reject guard uses this so it only ever compares real monthly figures against the
    monthly floor. Same HEURISTIC thresholds as ``normalize_monthly``: a yearly figure is divided by
    12; hourly / daily rates, extraction slips and absurd values come back as None, and the guard
    treats None as "unknown", never a reject. ``convert(amount, currency) -> RUB`` (an
    ``fx.FxRates.convert``) is needed for every currency but RUB; without it, or when it fails,
    the answer is None."""
    top = salary_max if salary_max is not None else salary_min
    cur = (currency or "").upper().strip()
    if top is None or not cur:
        return None
    top = float(top)
    if cur == "RUB":
        if top < RU_MONTH_MIN or top > RU_ANNUAL_MAX:
            return None
        return top if top <= RU_ANNUAL_MIN else top / 12.0
    if convert is None:
        return None
    try:
        top_rub, eur_rub = convert(top, cur), convert(1.0, "EUR")
    except Exception:  # noqa: BLE001 - a rate failure is "unknown", never a reject
        return None
    if not top_rub or not eur_rub:
        return None
    top_eur = top_rub / eur_rub
    if top_eur < INTL_MONTH_MIN_EUR or top_eur > INTL_ANNUAL_MAX_EUR:
        return None
    return top_rub if top_eur < INTL_ANNUAL_MIN_EUR else top_rub / 12.0


def _grade(seniority) -> Optional[str]:
    """Single grade from the extracted seniority text; 'middle+' counts as middle; mixed values -> None."""
    t = (seniority or "").lower().strip()
    if t == "middle+":
        return "middle"
    return t if t in GRADES else None


def _stats(vals: list, min_sample: int) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """(P25, median, P75) of per-vacancy values, rounded; all None below ``min_sample``. PURE."""
    if len(vals) < min_sample or not vals:
        return None, None, None
    sv = sorted(vals)
    return (int(round(_percentile(sv, 25))), int(round(_percentile(sv, 50))), int(round(_percentile(sv, 75))))


def _is_heuristic_row(row: dict) -> bool:
    """B2: a row read by the heuristic extractor (``extract_mode`` starts with ``heuristic``) stays out of EVERY pool.

    Its amount and currency came from regex, which the Stage 7 dry run showed unreliable (lost ``k`` suffix, missing
    currency symbol, dropped upper bound, ``Ruby`` read as RUB). ``llm``, an empty / NULL mode (old rows), a missing
    key and any other value count as before. The single rule for the RU and the international pool alike: every
    figure (P25 / median / P75, the middle-grade slices, the sample sizes, the composition diagnostics) is built
    from the rows this lets through."""
    return (row.get("extract_mode") or "").startswith("heuristic")


def _aggregate_salaries(
    salary_rows: list,
    min_sample: int,
    *,
    total_relevant: int = 0,
    rates_per_rub: Optional[Dict[str, float]] = None,
) -> MarketWorthResult:
    """Pure function: aggregate salary_rows into a MarketWorthResult.

    salary_rows: dicts with salary_min, salary_max, currency, source_channel and optionally
    seniority, geo_verdict and extract_mode (a ``heuristic*`` row is left out of both pools). ``rates_per_rub`` maps a currency to RUB per unit (FxRates shape);
    without it only EUR and RUB rows are usable. A row with geo_verdict "ineligible" is skipped.
    """
    from .clock import now_iso

    ru: list = []
    intl: list = []
    ru_mid: list = []
    intl_mid: list = []
    annual = {"ru": 0, "intl": 0}
    dropped: Dict[str, int] = {}
    geo_excluded = 0
    heuristic_excluded = {"ru": 0, "intl": 0}
    intl_geo_unchecked = 0
    intl_usd = 0
    intl_sources: Dict[str, int] = {}

    for row in salary_rows:
        if (row.get("geo_verdict") or "").lower() == "ineligible":
            geo_excluded += 1
            continue
        value, market, how = normalize_monthly(
            row.get("salary_min"), row.get("salary_max"), row.get("currency"), rates_per_rub
        )
        if value is None:
            if how not in ("no_currency", "no_value"):
                dropped[how] = dropped.get(how, 0) + 1
            continue
        if _is_heuristic_row(row):
            heuristic_excluded[market] += 1
            continue
        (ru if market == "ru" else intl).append(value)
        if market == "intl":
            if not (row.get("geo_verdict") or "").strip():
                intl_geo_unchecked += 1
            if (row.get("currency") or "").upper().strip() == "USD":
                intl_usd += 1
            src = (row.get("source_channel") or "").strip()
            if src:
                intl_sources[src] = intl_sources.get(src, 0) + 1
        if how == "annual":
            annual[market] += 1
        if _grade(row.get("seniority")) == "middle":
            (ru_mid if market == "ru" else intl_mid).append(value)

    ru_sample, intl_sample = len(ru), len(intl)
    top_src = max(intl_sources.items(), key=lambda kv: kv[1]) if intl_sources else None
    ru_min, ru_median, ru_max = _stats(ru, min_sample)
    intl_min, intl_median, intl_max = _stats(intl, min_sample)
    if ru_min is not None and ru_min == ru_max:
        ru_max = None  # single-cluster — show min only
    if intl_min is not None and intl_min == intl_max:
        intl_max = None
    rm_min, rm_med, rm_max = _stats(ru_mid, min_sample)
    im_min, im_med, im_max = _stats(intl_mid, min_sample)

    reasons: list = []
    if ru_sample < min_sample:
        reasons.append(f"Россия: {ru_sample} из {min_sample} вакансий с данными о зарплате")
    if intl_sample < min_sample:
        reasons.append(f"Международный: {intl_sample} из {min_sample} вакансий с данными о зарплате")
    degraded = bool(reasons)

    if degraded:
        reasoning = "Данных пока недостаточно для надёжной оценки. " + " | ".join(reasons) + ". Продолжаем собирать."
    else:
        reasoning = (
            f"По {ru_sample} РФ и {intl_sample} международным вакансиям со скором 25–100: "
            f"P25 / медиана / P75 по одной цифре на вакансию (середина вилки, в месяц; международные в EUR). "
            f"Период — эвристика."
        )

    return MarketWorthResult(
        ru_min=ru_min,
        ru_max=ru_max,
        ru_currency="RUB",
        intl_min=intl_min,
        intl_max=intl_max,
        intl_currency="EUR",
        ru_sample_size=ru_sample,
        intl_sample_size=intl_sample,
        min_sample=min_sample,
        total_relevant_vacancies=total_relevant,
        sources=["work_items pipeline"],
        reasoning_short=reasoning,
        computed_at=now_iso(),
        degraded=degraded,
        degraded_reason="; ".join(reasons) if reasons else None,
        ru_median=ru_median,
        intl_median=intl_median,
        ru_middle_min=rm_min,
        ru_middle_median=rm_med,
        ru_middle_max=rm_max,
        ru_middle_sample=len(ru_mid),
        intl_middle_min=im_min,
        intl_middle_median=im_med,
        intl_middle_max=im_max,
        intl_middle_sample=len(intl_mid),
        annual_converted=annual,
        dropped=dropped,
        geo_excluded=geo_excluded,
        ru_heuristic_excluded=heuristic_excluded["ru"],
        intl_heuristic_excluded=heuristic_excluded["intl"],
        intl_geo_unchecked=intl_geo_unchecked,
        intl_usd_share=round(intl_usd / intl_sample, 3) if intl_sample else None,
        intl_top_source=top_src[0] if top_src else None,
        intl_top_source_share=round(top_src[1] / intl_sample, 3) if top_src and intl_sample else None,
    )


# ---------------------------------------------------------------------------
# DB computation
# ---------------------------------------------------------------------------


def compute_from_pipeline(conn, cfg, fx=None) -> MarketWorthResult:
    """Query work_items with score 25–100 and aggregate salary data.

    Pure-SQL aggregation — zero API calls. Vacancies the geo guard judged INELIGIBLE are excluded
    (cards it never checked have a NULL verdict and stay in). ``fx`` is an ``fx.FxRates``; one is
    built from the config when omitted. If rates cannot be fetched, only EUR and RUB rows are usable."""
    min_sample = getattr(cfg, "market_min_sample", 5)

    rows = conn.execute(
        """
        SELECT extracted_json, relevance_score, source_channel, geo_verdict, extract_mode
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
            "seniority": e.get("seniority"),
            "source_channel": row["source_channel"] or "",
            "geo_verdict": row["geo_verdict"],
            "extract_mode": row["extract_mode"],
        })

    rates = None
    try:
        if fx is None:
            from . import fx as fx_mod

            fx = fx_mod.FxRates(provider=getattr(cfg, "fx_provider", "frankfurter"),
                                cache_ttl=getattr(cfg, "fx_cache_ttl", 86400))
        rates = fx.get_rates_per_rub()
    except Exception:  # noqa: BLE001 - no FX is a degraded benchmark, never a crash
        rates = None

    return _aggregate_salaries(salary_rows, min_sample, total_relevant=total_relevant, rates_per_rub=rates)


# ---------------------------------------------------------------------------
# Main entry point (compatibility wrapper — no cache, always fresh from DB)
# ---------------------------------------------------------------------------


def get_or_refresh(conn, cfg, fx=None) -> MarketWorthResult:
    """Compute a fresh MarketWorthResult from the pipeline DB.

    Pure computation — no logging side-effects. Callers that represent an
    explicit user action (POST /refresh, /worth bot command) can log after
    calling this if needed.
    """
    return compute_from_pipeline(conn, cfg, fx)


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
