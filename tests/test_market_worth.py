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
RATES = {"RUB": 1.0, "EUR": 100.0, "USD": 80.0, "GBP": 120.0}   # RUB per unit


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


def test_usd_goes_to_intl_market_converted_to_eur():
    rows = [_row(4_000, 7_000, "USD", "some_source")] * 3
    result = _aggregate_salaries(rows, MIN, rates_per_rub=RATES)
    assert result.intl_sample_size == 3
    assert result.intl_currency == "EUR"
    assert result.intl_min == round(5_500 * 80 / 100)        # midpoint 5 500 USD -> EUR at 80/100


def test_jobicy_source_goes_to_intl():
    """Jobicy source with EUR salary → intl market."""
    rows = [_row(3_500, 6_000, "EUR", "jobicy_europe")] * 3
    result = _aggregate_salaries(rows, MIN)
    assert result.intl_sample_size == 3


def test_mixed_currencies_are_converted_not_mixed_as_raw_numbers():
    """EUR and USD used to be pooled as raw numbers and labelled with the majority currency."""
    rows = [_row(5_000, 5_000, "EUR", "j")] * 3 + [_row(5_000, 5_000, "USD", "j")] * 3
    result = _aggregate_salaries(rows, min_sample=6, rates_per_rub=RATES)
    assert result.intl_currency == "EUR"
    assert result.intl_min == 4_000 and result.intl_max == 5_000      # USD 5 000 = EUR 4 000


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


# ---------------------------------------------------------------------------
# T7: period / currency normalisation (HEURISTIC), median, grade slice, geo, drops
# ---------------------------------------------------------------------------

from job_hunter.market_worth import (  # noqa: E402
    HEURISTIC_NOTE,
    INTL_ANNUAL_MIN_EUR,
    normalize_monthly,
)


def test_heuristic_is_labelled_as_such():
    assert "Эвристика" in HEURISTIC_NOTE and "gross" in HEURISTIC_NOTE.lower()
    assert _aggregate_salaries([], MIN).heuristic_note == HEURISTIC_NOTE
    full = _aggregate_salaries([_ru(200_000, 300_000)] * 3 + [_intl(4_000, 6_000)] * 3, MIN)
    assert not full.degraded and "эвристика" in full.reasoning_short.lower()


def test_yearly_intl_figures_are_divided_by_twelve():
    value, market, how = normalize_monthly(96_000, 144_000, "EUR", RATES)
    assert (value, market, how) == (10_000.0, "intl", "annual")
    assert normalize_monthly(4_000, 6_000, "EUR", RATES)[2] == "monthly"


def test_period_threshold_boundary_on_the_eur_equivalent_of_the_top():
    below = normalize_monthly(None, INTL_ANNUAL_MIN_EUR - 1, "EUR", RATES)
    at = normalize_monthly(None, INTL_ANNUAL_MIN_EUR, "EUR", RATES)
    assert below[2] == "monthly" and at[2] == "annual"
    # USD is judged on its EUR equivalent: 24 000 USD = 19 200 EUR -> still monthly
    assert normalize_monthly(None, 24_000, "USD", RATES)[2] == "monthly"
    assert normalize_monthly(None, 26_000, "USD", RATES)[2] == "annual"


def test_range_straddling_the_threshold_uses_one_period_for_both_bounds():
    value, _, how = normalize_monthly(18_000, 30_000, "EUR", RATES)
    assert how == "annual" and value == pytest.approx(24_000 / 12)


def test_implausible_values_are_dropped_with_a_reason():
    assert normalize_monthly(None, 50, "USD", RATES)[2] == "implausible_low"          # hourly / slip
    assert normalize_monthly(None, 781_000_000, "USD", RATES)[2] == "implausible_high"
    assert normalize_monthly(None, 15_000, "RUB", RATES)[2] == "implausible_low"
    assert normalize_monthly(None, 25_000_000, "RUB", RATES)[2] == "implausible_high"
    assert normalize_monthly(None, 1_000, "SEK", RATES)[2] == "no_rate"
    assert normalize_monthly(None, 1_000, "", RATES)[2] == "no_currency"


def test_yearly_rub_figures_are_divided_by_twelve():
    assert normalize_monthly(2_400_000, 3_600_000, "RUB", RATES)[:3] == (250_000.0, "ru", "annual")
    assert normalize_monthly(200_000, 300_000, "RUB", RATES)[2] == "monthly"


def test_without_rates_only_eur_and_rub_rows_are_usable():
    rows = [_row(5_000, 5_000, "USD", "j")] * 3 + [_row(5_000, 5_000, "EUR", "j")] * 3
    result = _aggregate_salaries(rows, MIN)
    assert result.intl_sample_size == 3 and result.dropped == {"no_rate": 3}


def test_each_vacancy_counts_once_and_the_median_is_reported():
    rows = [_row(100_000, 300_000, "RUB", "c"), _row(200_000, 200_000, "RUB", "c"), _row(150_000, None, "RUB", "c")]
    result = _aggregate_salaries(rows, MIN)
    assert result.ru_sample_size == 3                       # midpoints 200k, 200k, 150k
    assert result.ru_median == 200_000
    assert result.ru_min == 175_000 and result.ru_max == 200_000


def test_geo_ineligible_vacancies_are_excluded_and_counted():
    rows = [dict(_ru(200_000, 300_000), geo_verdict="ineligible")] * 2 + [dict(_ru(200_000, 300_000), geo_verdict=v)
                                                                      for v in ("eligible", "unknown", None)]
    result = _aggregate_salaries(rows, MIN)
    assert result.ru_sample_size == 3 and result.geo_excluded == 2


def test_middle_grade_slice_and_unknown_seniority():
    rows = (
        [dict(_ru(200_000, 200_000), seniority="middle")] * 2
        + [dict(_ru(250_000, 250_000), seniority="middle+")]
        + [dict(_ru(400_000, 400_000), seniority="senior")] * 3
        + [dict(_ru(100_000, 100_000), seniority="junior|middle|senior")]      # mixed -> no grade
        + [_ru(90_000, 90_000)]                                                # missing seniority
    )
    result = _aggregate_salaries(rows, MIN)
    assert result.ru_sample_size == 8
    assert result.ru_middle_sample == 3 and result.ru_middle_median == 200_000
    thin = _aggregate_salaries(rows[:2] + rows[3:], 3)                         # only 2 middle -> too few
    assert thin.ru_middle_sample == 2 and thin.ru_middle_median is None
