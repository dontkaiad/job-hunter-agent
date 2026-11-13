"""Tests for job_hunter.stack_analytics — pure aggregation, no DB or API calls.

Uses _aggregate_stack() directly (pure function) to avoid DB dependency.
"""

import pytest

from job_hunter.stack_analytics import (
    StackAnalyticsResult,
    _aggregate_stack,
    _display_name,
    _normalize_key,
)

MIN = 5  # default min_display_sample used in most tests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(stacks, *, total_pool=None, min_sample=MIN, top_n=20):
    tp = total_pool if total_pool is not None else len(stacks)
    return _aggregate_stack(stacks, total_pool=tp, min_display_sample=min_sample, top_n=top_n)


def _freq(result: StackAnalyticsResult) -> dict:
    return result.tech_freq


def _top_techs(result: StackAnalyticsResult):
    return [item["tech"] for item in result.top_tech]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_key_lowercases():
    assert _normalize_key("Python") == "python"
    assert _normalize_key("  DOCKER  ") == "docker"


def test_display_name_canonical():
    assert _display_name("python") == "Python"
    assert _display_name("pytorch") == "PyTorch"
    assert _display_name("postgres") == "PostgreSQL"
    assert _display_name("langchain") == "LangChain"


def test_display_name_unknown_preserves_input():
    assert _display_name("SomeCoolLib") == "SomeCoolLib"


# ---------------------------------------------------------------------------
# Basic aggregation
# ---------------------------------------------------------------------------


def test_empty_pool():
    r = _result([])
    assert r.total_pool == 0
    assert r.vacancies_with_stack == 0
    assert r.tech_freq == {}
    assert r.top_tech == []


def test_single_vacancy_counts():
    r = _result([["Python", "Docker"]])
    assert r.vacancies_with_stack == 1
    assert _freq(r)["Python"] == 1
    assert _freq(r)["Docker"] == 1


def test_frequency_sorted_desc():
    stacks = [
        ["Python", "Docker"],
        ["Python", "LangChain"],
        ["Python"],
    ]
    r = _result(stacks)
    tops = _top_techs(r)
    assert tops[0] == "Python"  # appears in all 3
    assert _freq(r)["Python"] == 3
    assert _freq(r)["Docker"] == 1
    assert _freq(r)["LangChain"] == 1


def test_pct_calculation():
    stacks = [["Python", "Docker"], ["Python"]]
    r = _result(stacks)
    python_item = next(i for i in r.top_tech if i["tech"] == "Python")
    assert python_item["pct"] == 100.0
    docker_item = next(i for i in r.top_tech if i["tech"] == "Docker")
    assert docker_item["pct"] == 50.0


# ---------------------------------------------------------------------------
# Deduplication — tech counted once per vacancy even if repeated
# ---------------------------------------------------------------------------


def test_dedup_within_vacancy():
    stacks = [["Python", "python", "PYTHON"]]
    r = _result(stacks)
    # all three normalize to "python" → displayed as "Python", counted once
    assert _freq(r).get("Python", 0) == 1
    assert r.vacancies_with_stack == 1


# ---------------------------------------------------------------------------
# Empty/None stacks
# ---------------------------------------------------------------------------


def test_empty_stack_lists_excluded():
    stacks = [[], ["Python"], [], ["Docker"]]
    r = _result(stacks, total_pool=4)
    assert r.vacancies_with_stack == 2
    assert r.total_pool == 4
    assert _freq(r)["Python"] == 1
    assert _freq(r)["Docker"] == 1


def test_non_string_items_skipped():
    stacks = [[None, 42, "Python", ""]]
    r = _result(stacks)
    # only "Python" is valid
    assert list(_freq(r).keys()) == ["Python"]


# ---------------------------------------------------------------------------
# Canonical alias merging
# ---------------------------------------------------------------------------


def test_postgres_alias_merges():
    stacks = [["postgres"], ["PostgreSQL"]]
    r = _result(stacks)
    # both map to canonical "PostgreSQL"
    assert _freq(r).get("PostgreSQL") == 2
    # should NOT have a separate "postgres" key
    assert "postgres" not in _freq(r)


# ---------------------------------------------------------------------------
# top_n slicing
# ---------------------------------------------------------------------------


def test_top_n_limits_output():
    stacks = [[f"Tech{i}" for i in range(30)]]
    r = _aggregate_stack(stacks, total_pool=1, top_n=10)
    assert len(r.top_tech) == 10


def test_top_n_includes_most_frequent():
    stacks = [["Rare"]] + [["Common"]] * 10
    r = _aggregate_stack(stacks, total_pool=11, top_n=1)
    assert r.top_tech[0]["tech"] == "Common"


# ---------------------------------------------------------------------------
# small_sample flag
# ---------------------------------------------------------------------------


def test_small_sample_flagged():
    stacks = [["Python"]] * 3
    r = _result(stacks, total_pool=3, min_sample=5)
    assert r.small_sample is True
    assert r.degraded_reason is not None
    assert "3" in r.degraded_reason


def test_small_sample_not_flagged_at_threshold():
    stacks = [["Python"]] * 5
    r = _result(stacks, total_pool=5, min_sample=5)
    assert r.small_sample is False
    assert r.degraded_reason is None


# ---------------------------------------------------------------------------
# vacancies_with_stack counting
# ---------------------------------------------------------------------------


def test_vacancies_with_stack_counts_non_empty():
    stacks = [["Python"], [], ["Docker", "Redis"], []]
    r = _result(stacks, total_pool=4)
    assert r.vacancies_with_stack == 2


# ---------------------------------------------------------------------------
# tech_freq is a reusable plain dict
# ---------------------------------------------------------------------------


def test_tech_freq_is_dict():
    stacks = [["Python", "Docker"]]
    r = _result(stacks)
    assert isinstance(r.tech_freq, dict)
    assert r.tech_freq["Python"] == 1


# ---------------------------------------------------------------------------
# 2c: Jenkins, 1C, Bitrix24 (invented texts only: the repository is public)
# ---------------------------------------------------------------------------

LAT_C = "1C"
CYR_S = "1С"                       # "1С" with a CYRILLIC capital Es
CYR_s = "1с"                       # "1с" with a cyrillic small es
ENT_RU = "1С:Предприятие"            # 1С:Предприятие
BITRIX_RU = "Битрикс24"                                       # Битрикс24
BITRIX_RU_SP = "Битрикс 24"                                   # Битрикс 24


@pytest.mark.parametrize("raw,expected", [
    ("Jenkins", "Jenkins"), ("jenkins", "Jenkins"), ("JENKINS", "Jenkins"), (" Jenkins ", "Jenkins"),
    (LAT_C, "1C"), ("1c", "1C"), (CYR_S, "1C"), (CYR_s, "1C"),
    ("1C:Enterprise", "1C"), ("1c:enterprise", "1C"), ("1C Enterprise", "1C"), (ENT_RU, "1C"),
    ("1с:enterprise", "1C"),
    ("Bitrix24", "Bitrix24"), ("bitrix24", "Bitrix24"), ("Bitrix 24", "Bitrix24"), ("Bitrix  24", "Bitrix24"),
    (BITRIX_RU, "Bitrix24"), (BITRIX_RU_SP, "Bitrix24"), (BITRIX_RU.lower(), "Bitrix24"),
])
def test_new_terms_get_one_canonical_name(raw, expected):
    assert _display_name(raw) == expected


@pytest.mark.parametrize("raw", [
    "1 с лишним",          # "1 с лишним": a phrase, not the product
    "1с/сек", "1c/sec", "1 c", "11C", "C1", "1СС",
    "Jenkinsfile", "Jenkins Smith", "Jenkin", "Bitrix", "Bitrix2400", "Bitrix 240",
    "Битрикс", "битриксы 24",
])
def test_lookalikes_are_not_mapped(raw):
    assert _display_name(raw) == raw.strip()


def test_spellings_of_one_term_merge_in_the_count():
    rows = [["Jenkins"], ["jenkins"], [LAT_C], [CYR_S], [ENT_RU], ["Bitrix24"], [BITRIX_RU], ["Bitrix 24"]]
    f = _freq(_result(rows))
    assert f["Jenkins"] == 2 and f["1C"] == 3 and f["Bitrix24"] == 3
    assert not any(k.lower() in ("jenkins", "1c", "1с") and k not in ("Jenkins", "1C") for k in f)


def test_two_spellings_in_one_vacancy_count_once():
    f = _freq(_result([[LAT_C, CYR_S, ENT_RU, "Jenkins", "jenkins"]]))
    assert f == {"1C": 1, "Jenkins": 1}


def test_other_terms_are_unchanged_by_the_new_entries():
    f = _freq(_result([["Python", "redis", "KAFKA", "Jenkins"], ["python", "Redis"]]))
    assert f["Python"] == 2 and f["Redis"] == 2 and f["Kafka"] == 1 and f["Jenkins"] == 1
