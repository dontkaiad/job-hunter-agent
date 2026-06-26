"""Tests for job_hunter.requirements_analytics — pure aggregation, no DB."""

from job_hunter.requirements_analytics import (
    RequirementsAnalyticsResult,
    _aggregate_requirements,
    _remote_key,
    HYBRID_TAG,
    GRADE_ORDER,
)

MIN = 5


def _result(rows, *, total_pool=None, min_sample=MIN):
    tp = total_pool if total_pool is not None else len(rows)
    return _aggregate_requirements(rows, total_pool=tp, min_display_sample=min_sample)


def _row(seniority=None, remote=None, location=None, relocation=None):
    return {"seniority": seniority, "remote": remote, "location": location, "relocation": relocation}


def _grades(r: RequirementsAnalyticsResult):
    return [d["grade"] for d in r.seniority_dist]


def _remote_keys(r: RequirementsAnalyticsResult):
    return [d["key"] for d in r.remote_dist if d["count"] > 0]


# ---------------------------------------------------------------------------
# _remote_key helper
# ---------------------------------------------------------------------------


def test_remote_key_fully_remote():
    assert _remote_key(True, None) == "remote"
    assert _remote_key(True, "Москва") == "remote"


def test_remote_key_hybrid():
    assert _remote_key(True, HYBRID_TAG) == "hybrid"
    assert _remote_key(True, f"Москва {HYBRID_TAG}") == "hybrid"


def test_remote_key_office():
    assert _remote_key(False, None) == "office"
    assert _remote_key(False, "СПб") == "office"


def test_remote_key_unknown():
    assert _remote_key(None, None) == "unknown"
    assert _remote_key(None, "Berlin") == "unknown"


# ---------------------------------------------------------------------------
# Empty pool
# ---------------------------------------------------------------------------


def test_empty_pool():
    r = _result([])
    assert r.total_pool == 0
    assert r.seniority_freq == {}
    assert r.seniority_dist == []
    assert r.relocation_count == 0
    assert r.relocation_pct == 0.0


# ---------------------------------------------------------------------------
# Seniority frequency
# ---------------------------------------------------------------------------


def test_seniority_counted():
    rows = [_row("senior"), _row("middle"), _row("senior")]
    r = _result(rows)
    assert r.seniority_freq["senior"] == 2
    assert r.seniority_freq["middle"] == 1
    assert r.vacancies_with_seniority == 3


def test_seniority_none_excluded():
    rows = [_row(None), _row("senior"), _row(None)]
    r = _result(rows)
    assert r.vacancies_with_seniority == 1
    assert "senior" in r.seniority_freq


def test_seniority_case_insensitive():
    rows = [_row("Senior"), _row("MIDDLE"), _row("junior")]
    r = _result(rows)
    assert r.seniority_freq.get("senior") == 1
    assert r.seniority_freq.get("middle") == 1
    assert r.seniority_freq.get("junior") == 1


def test_seniority_pct():
    rows = [_row("senior"), _row("senior"), _row("middle")]
    r = _result(rows)
    senior = next(d for d in r.seniority_dist if d["grade"] == "senior")
    assert senior["pct"] == round(2 / 3 * 100, 1)


def test_seniority_dist_grade_order():
    # Known grades should appear in GRADE_ORDER sequence
    rows = [_row("senior"), _row("junior"), _row("middle"), _row("lead")]
    r = _result(rows)
    known = [d["grade"] for d in r.seniority_dist if d["grade"] in GRADE_ORDER]
    expected_order = [g for g in GRADE_ORDER if g in {"junior", "middle", "senior", "lead"}]
    assert known == expected_order


def test_seniority_unknown_grade_appended():
    rows = [_row("principal"), _row("senior")]
    r = _result(rows)
    grades = _grades(r)
    assert "senior" in grades
    assert "principal" in grades
    # "principal" not in GRADE_ORDER, should come after known grades
    assert grades.index("senior") < grades.index("principal")


# ---------------------------------------------------------------------------
# Remote distribution
# ---------------------------------------------------------------------------


def test_remote_distribution_basic():
    rows = [
        _row(remote=True),                            # remote
        _row(remote=True, location=HYBRID_TAG),       # hybrid
        _row(remote=False),                           # office
        _row(remote=None),                            # unknown
    ]
    r = _result(rows, total_pool=4)
    rkeys = {d["key"]: d["count"] for d in r.remote_dist}
    assert rkeys["remote"] == 1
    assert rkeys["hybrid"] == 1
    assert rkeys["office"] == 1
    assert rkeys["unknown"] == 1


def test_remote_pct_uses_total_pool():
    rows = [_row(remote=True), _row(remote=True)]
    r = _result(rows, total_pool=4)
    remote_entry = next(d for d in r.remote_dist if d["key"] == "remote")
    assert remote_entry["pct"] == 50.0


def test_remote_dist_sorted_by_count_desc():
    rows = [_row(remote=True)] * 5 + [_row(remote=False)] * 2 + [_row(remote=None)]
    r = _result(rows, total_pool=8)
    counts = [d["count"] for d in r.remote_dist]
    assert counts == sorted(counts, reverse=True)


def test_all_remote():
    rows = [_row(remote=True)] * 3
    r = _result(rows, total_pool=3)
    remote_entry = next(d for d in r.remote_dist if d["key"] == "remote")
    assert remote_entry["pct"] == 100.0


# ---------------------------------------------------------------------------
# Relocation
# ---------------------------------------------------------------------------


def test_relocation_counted():
    rows = [_row(relocation=True), _row(relocation=True), _row(relocation=False), _row()]
    r = _result(rows, total_pool=4)
    assert r.relocation_count == 2
    assert r.relocation_pct == 50.0


def test_relocation_none_not_counted():
    rows = [_row(relocation=None), _row(relocation=False)]
    r = _result(rows, total_pool=2)
    assert r.relocation_count == 0
    assert r.relocation_pct == 0.0


def test_relocation_pct_zero_pool():
    r = _result([], total_pool=0)
    assert r.relocation_pct == 0.0


# ---------------------------------------------------------------------------
# small_sample flag
# ---------------------------------------------------------------------------


def test_small_sample_flagged():
    rows = [_row("senior")] * 3
    r = _result(rows, total_pool=3, min_sample=5)
    assert r.small_sample is True
    assert "3" in r.degraded_reason


def test_small_sample_not_flagged_at_threshold():
    rows = [_row("senior")] * 5
    r = _result(rows, total_pool=5, min_sample=5)
    assert r.small_sample is False
    assert r.degraded_reason is None


# ---------------------------------------------------------------------------
# Combined row
# ---------------------------------------------------------------------------


def test_full_row_aggregated():
    rows = [
        _row("middle", remote=True, location=None, relocation=True),
        _row("senior", remote=False, location="Москва", relocation=False),
        _row("middle", remote=True, location=HYBRID_TAG, relocation=True),
    ]
    r = _result(rows, total_pool=3)
    assert r.seniority_freq["middle"] == 2
    assert r.seniority_freq["senior"] == 1
    assert r.relocation_count == 2
    rkeys = {d["key"]: d["count"] for d in r.remote_dist}
    assert rkeys["remote"] == 1
    assert rkeys["hybrid"] == 1
    assert rkeys["office"] == 1
