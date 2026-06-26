"""Tests for job_hunter.market_worth — pure aggregation, no DB or API calls.

Uses _aggregate_salaries() directly (pure function) to avoid DB dependency.
"""

from types import SimpleNamespace

import pytest

from job_hunter.market_worth import (
    MarketWorthResult,
    _aggregate_salaries,
    fmt_range,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(salary_min=None, salary_max=None, currency="RUB", source_channel="tg_ch"):
    return {
        "salary_min": salary_min,
        "salary_max": salary_max,
        "currency": currency,
        "source_channel": source_channel,
    }


def _ru(mn, mx):
    return _row(mn, mx, "RUB")


def _intl(mn, mx, currency="EUR"):
    return _row(mn, mx, currency, source_channel="jobicy_eu")


MIN = 3  # default min_sample in tests


# ---------------------------------------------------------------------------
# T1: fmt_range
# ---------------------------------------------------------------------------


def test_fmt_range_both():
    assert "150" in fmt_range(150_000, 250_000, "RUB")
    assert "₽" in fmt_range(150_000, 250_000, "RUB")


def test_fmt_range_min_only():
    result = fmt_range(3_000, None, "EUR")
    assert "от" in result
    assert "€" in result
    assert "потолок" in result


def test_fmt_range_none():
    assert fmt_range(None, None, "EUR") == "нет данных"


def test_fmt_range_max_only():
    result = fmt_range(None, 5_000, "USD")
    assert "до" in result
    assert "$" in result


# ---------------------------------------------------------------------------
# T2: _aggregate_salaries — empty / insufficient data → degraded
# ---------------------------------------------------------------------------


def test_empty_rows_degraded():
    result = _aggregate_salaries([], MIN)
    assert result.degraded
    assert result.ru_min is None
    assert result.intl_min is None
    assert result.ru_sample_size == 0
    assert result.intl_sample_size == 0


def test_insufficient_ru_sample_degraded():
    rows = [_ru(150_000, 250_000), _ru(180_000, 280_000)]  # 2 < 3
    result = _aggregate_salaries(rows, MIN)
    assert result.degraded
    assert result.ru_min is None
    assert "Россия" in result.degraded_reason


def test_insufficient_intl_sample_degraded():
    rows = [_intl(3_000, 6_000), _intl(4_000, 7_000)]  # 2 < 3
    result = _aggregate_salaries(rows, MIN)
    assert result.degraded
    assert result.intl_min is None
    assert "Международный" in result.degraded_reason


def test_no_salary_field_excluded():
    """Rows with no salary data don't count toward sample."""
    rows = [_row(None, None, "RUB")] * 5
    result = _aggregate_salaries(rows, MIN)
    assert result.degraded
    assert result.ru_sample_size == 0


def test_no_currency_excluded():
    rows = [_row(100_000, 200_000, "")] * 5
    result = _aggregate_salaries(rows, MIN)
    assert result.degraded  # unknown currency — not counted for either market


# ---------------------------------------------------------------------------
# T3: _aggregate_salaries — sufficient sample → real range
# ---------------------------------------------------------------------------


def test_sufficient_ru_sample_not_degraded():
    rows = [_ru(150_000, 250_000), _ru(170_000, 270_000), _ru(160_000, 260_000)]
    result = _aggregate_salaries(rows, MIN)
    # Only RU is sufficient; intl is 0
    assert result.ru_min is not None
    assert result.ru_max is not None
    assert result.ru_sample_size == 3


def test_sufficient_intl_sample():
    rows = [_intl(3_000, 6_000), _intl(4_000, 7_000), _intl(3_500, 5_500)]
    result = _aggregate_salaries(rows, MIN)
    assert result.intl_min is not None
    assert result.intl_max is not None
    assert result.intl_sample_size == 3


def test_both_markets_sufficient_not_degraded():
    rows = (
        [_ru(150_000, 250_000)] * 3
        + [_intl(3_000, 6_000)] * 3
    )
    result = _aggregate_salaries(rows, MIN)
    assert not result.degraded
    assert result.ru_min is not None
    assert result.intl_min is not None


def test_range_uses_percentiles():
    """P25-P75 excludes outliers."""
    # 4 values: 100, 200, 200, 1000 → P25≈125, P75≈350
    rows = [
        _ru(100_000, None),
        _ru(200_000, None),
        _ru(200_000, None),
        _ru(1_000_000, None),
    ]
    result = _aggregate_salaries(rows, min_sample=4)
    assert result.ru_min is not None
    # P25 should exclude the outlier 1M (P75=~350k, not 1M)
    assert result.ru_max is not None
    assert result.ru_max < 1_000_000


def test_min_only_salary_counts():
    """A row with only salary_min still contributes to sample."""
    rows = [_row(200_000, None, "RUB")] * 3
    result = _aggregate_salaries(rows, MIN)
    assert result.ru_sample_size == 3
    assert result.ru_min is not None


def test_max_only_salary_counts():
    rows = [_row(None, 300_000, "RUB")] * 3
    result = _aggregate_salaries(rows, MIN)
    assert result.ru_sample_size == 3


# ---------------------------------------------------------------------------
# T4: currency / market routing
# ---------------------------------------------------------------------------


def test_rub_goes_to_ru_market():
    rows = [_row(200_000, 300_000, "RUB", "any_channel")] * 3
    result = _aggregate_salaries(rows, MIN)
    assert result.ru_sample_size == 3
    assert result.intl_sample_size == 0


def test_eur_goes_to_intl_market():
    rows = [_row(4_000, 7_000, "EUR", "some_source")] * 3
    result = _aggregate_salaries(rows, MIN)
    assert result.intl_sample_size == 3
    assert result.ru_sample_size == 0
    assert result.intl_currency == "EUR"


def test_usd_goes_to_intl_market():
    rows = [_row(4_000, 7_000, "USD", "some_source")] * 3
    result = _aggregate_salaries(rows, MIN)
    assert result.intl_sample_size == 3
    assert result.intl_currency == "USD"


def test_jobicy_source_goes_to_intl():
    """Jobicy source with EUR salary → intl market."""
    rows = [_row(3_500, 6_000, "EUR", "jobicy_europe")] * 3
    result = _aggregate_salaries(rows, MIN)
    assert result.intl_sample_size == 3


def test_intl_currency_majority_wins():
    """Predominant currency wins when mixed."""
    rows = (
        [_row(4_000, 7_000, "EUR", "j")] * 4
        + [_row(4_500, 8_000, "USD", "j")] * 2
    )
    result = _aggregate_salaries(rows, min_sample=6)
    assert result.intl_currency == "EUR"


# ---------------------------------------------------------------------------
# T5: total_relevant_vacancies pass-through
# ---------------------------------------------------------------------------


def test_total_relevant_vacancies():
    rows = [_ru(150_000, 250_000)] * 3
    result = _aggregate_salaries(rows, MIN, total_relevant=42)
    assert result.total_relevant_vacancies == 42


# ---------------------------------------------------------------------------
# T6: result structure
# ---------------------------------------------------------------------------


def test_result_has_required_fields():
    result = _aggregate_salaries([], MIN)
    assert isinstance(result, MarketWorthResult)
    assert result.computed_at  # ISO-8601 string
    assert result.sources == ["work_items pipeline"]
    assert result.min_sample == MIN
