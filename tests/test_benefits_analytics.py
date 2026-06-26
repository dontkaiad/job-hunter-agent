"""Tests for job_hunter.benefits_analytics — pure aggregation, no DB or API calls."""

from job_hunter.benefits_analytics import (
    BenefitsAnalyticsResult,
    _aggregate_benefits,
    _label_to_canonical,
    _display_name,
)

MIN = 5


def _result(rows, *, total_pool=None, min_sample=MIN):
    tp = total_pool if total_pool is not None else len(rows)
    return _aggregate_benefits(rows, total_pool=tp, min_display_sample=min_sample)


def _freq(r: BenefitsAnalyticsResult) -> dict:
    return r.benefits_freq


def _top_names(r: BenefitsAnalyticsResult):
    return [item["benefit"] for item in r.top_benefits]


# ---------------------------------------------------------------------------
# Label mapping
# ---------------------------------------------------------------------------


def test_label_to_canonical_ru():
    assert _label_to_canonical("ДМС / медстраховка") == "health_insurance"
    assert _label_to_canonical("Удалёнка") == "remote_perk"
    assert _label_to_canonical("Бонусы / премии") == "bonus"


def test_label_to_canonical_en():
    assert _label_to_canonical("Health insurance") == "health_insurance"
    assert _label_to_canonical("Remote work") == "remote_perk"
    assert _label_to_canonical("Bonuses") == "bonus"


def test_label_to_canonical_case_insensitive():
    assert _label_to_canonical("дмс / медстраховка") == "health_insurance"
    assert _label_to_canonical("REMOTE WORK") == "remote_perk"


def test_label_to_canonical_unknown_returns_none():
    assert _label_to_canonical("Крипто в зарплате") is None


def test_display_name_returns_ru():
    assert _display_name("health_insurance") == "ДМС / медстраховка"
    assert _display_name("bonus") == "Бонусы / премии"


# ---------------------------------------------------------------------------
# Basic aggregation
# ---------------------------------------------------------------------------


def test_empty_pool():
    r = _result([])
    assert r.total_pool == 0
    assert r.vacancies_with_benefits == 0
    assert r.benefits_freq == {}
    assert r.top_benefits == []


def test_single_vacancy_single_benefit():
    r = _result([["ДМС / медстраховка"]])
    assert r.vacancies_with_benefits == 1
    assert _freq(r)["health_insurance"] == 1


def test_frequency_sorted_desc():
    rows = [
        ["ДМС / медстраховка", "Удалёнка"],
        ["ДМС / медстраховка", "Бонусы / премии"],
        ["ДМС / медстраховка"],
    ]
    r = _result(rows)
    assert _top_names(r)[0] == "ДМС / медстраховка"
    assert _freq(r)["health_insurance"] == 3
    assert _freq(r)["remote_perk"] == 1
    assert _freq(r)["bonus"] == 1


def test_pct_calculation():
    rows = [["ДМС / медстраховка", "Удалёнка"], ["ДМС / медстраховка"]]
    r = _result(rows)
    dms = next(i for i in r.top_benefits if i["canonical"] == "health_insurance")
    remote = next(i for i in r.top_benefits if i["canonical"] == "remote_perk")
    assert dms["pct"] == 100.0
    assert remote["pct"] == 50.0


# ---------------------------------------------------------------------------
# Dedup — benefit counted once per vacancy even if repeated
# ---------------------------------------------------------------------------


def test_dedup_same_label_within_vacancy():
    rows = [["ДМС / медстраховка", "ДМС / медстраховка"]]
    r = _result(rows)
    assert _freq(r)["health_insurance"] == 1


def test_dedup_ru_and_en_same_canonical():
    # RU and EN labels for same benefit in one vacancy → counted once
    rows = [["ДМС / медстраховка", "Health insurance"]]
    r = _result(rows)
    assert _freq(r)["health_insurance"] == 1
    assert r.vacancies_with_benefits == 1


# ---------------------------------------------------------------------------
# Unknown labels silently skipped
# ---------------------------------------------------------------------------


def test_unknown_label_skipped():
    rows = [["Крипто-опционы", "ДМС / медстраховка"]]
    r = _result(rows)
    assert list(_freq(r).keys()) == ["health_insurance"]


def test_non_string_items_skipped():
    rows = [[None, 42, "ДМС / медстраховка", ""]]
    r = _result(rows)
    assert list(_freq(r).keys()) == ["health_insurance"]


# ---------------------------------------------------------------------------
# Empty / None rows
# ---------------------------------------------------------------------------


def test_empty_benefit_lists_excluded():
    rows = [[], ["ДМС / медстраховка"], [], ["Удалёнка"]]
    r = _result(rows, total_pool=4)
    assert r.vacancies_with_benefits == 2
    assert r.total_pool == 4


def test_all_empty_rows():
    r = _result([[], [], []], total_pool=3)
    assert r.vacancies_with_benefits == 0
    assert r.benefits_freq == {}


# ---------------------------------------------------------------------------
# small_sample flag
# ---------------------------------------------------------------------------


def test_small_sample_flagged():
    rows = [["ДМС / медстраховка"]] * 3
    r = _result(rows, total_pool=3, min_sample=5)
    assert r.small_sample is True
    assert "3" in r.degraded_reason


def test_small_sample_not_flagged_at_threshold():
    rows = [["ДМС / медстраховка"]] * 5
    r = _result(rows, total_pool=5, min_sample=5)
    assert r.small_sample is False
    assert r.degraded_reason is None


# ---------------------------------------------------------------------------
# benefits_freq is a reusable plain dict
# ---------------------------------------------------------------------------


def test_benefits_freq_is_dict():
    r = _result([["ДМС / медстраховка", "Удалёнка"]])
    assert isinstance(r.benefits_freq, dict)
    assert r.benefits_freq["health_insurance"] == 1
    assert r.benefits_freq["remote_perk"] == 1


# ---------------------------------------------------------------------------
# Mixed RU/EN vacancies aggregate to same canonical key
# ---------------------------------------------------------------------------


def test_mixed_language_vacancies_merge():
    rows = [
        ["Health insurance"],          # EN vacancy
        ["ДМС / медстраховка"],        # RU vacancy
        ["Health insurance", "Bonuses"],
    ]
    r = _result(rows)
    assert _freq(r)["health_insurance"] == 3
    assert _freq(r)["bonus"] == 1
