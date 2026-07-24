"""Tests for job_hunter.competencies — pure parse + market sync, no DB/API."""

from job_hunter.competencies import (
    BUCKETS,
    CompetencyEntry,
    parse_competencies,
    sync_with_market,
)

# ---------------------------------------------------------------------------
# parse_competencies
# ---------------------------------------------------------------------------


def test_parse_empty_data_returns_empty():
    assert parse_competencies({}) == []
    assert parse_competencies(None) == []
    assert parse_competencies("not a mapping") == []


def test_parse_reads_all_four_buckets():
    data = {
        "core": [{"term_en": "A"}],
        "growing": [{"term_en": "B"}],
        "skip": [{"term_en": "C"}],
        "glossary": [{"term_en": "D"}],
    }
    entries = parse_competencies(data)
    assert {e.bucket for e in entries} == set(BUCKETS)
    assert len(entries) == 4


def test_parse_skips_row_missing_both_terms():
    data = {"core": [{"explainer_en": "no terms here"}]}
    assert parse_competencies(data) == []


def test_parse_skips_non_mapping_rows():
    data = {"core": ["not a dict", 42, None]}
    assert parse_competencies(data) == []


def test_parse_ignores_unknown_bucket_key():
    data = {"core": [{"term_en": "A"}], "made_up_bucket": [{"term_en": "X"}]}
    entries = parse_competencies(data)
    assert len(entries) == 1
    assert entries[0].term_en == "A"


def test_parse_fills_all_fields():
    data = {
        "core": [
            {
                "term_ru": "Термин",
                "term_en": "Term",
                "explainer_ru": "30 сек",
                "explainer_en": "30 sec",
                "resume_line": "Did X",
                "project": "job-hunter-agent",
                "source_ref": "job_hunter/foo.py:bar",
            }
        ]
    }
    e = parse_competencies(data)[0]
    assert e == CompetencyEntry(
        bucket="core",
        term_ru="Термин",
        term_en="Term",
        explainer_ru="30 сек",
        explainer_en="30 sec",
        resume_line="Did X",
        project="job-hunter-agent",
        source_ref="job_hunter/foo.py:bar",
    )


def test_parse_term_ru_only_is_kept():
    data = {"core": [{"term_ru": "Только по-русски"}]}
    entries = parse_competencies(data)
    assert len(entries) == 1
    assert entries[0].term_ru == "Только по-русски"
    assert entries[0].term_en == ""


# ---------------------------------------------------------------------------
# sync_with_market
# ---------------------------------------------------------------------------


def _entry(bucket, term_en, term_ru=""):
    return CompetencyEntry(bucket=bucket, term_ru=term_ru, term_en=term_en)


def test_sync_matches_known_term_case_insensitive():
    entries = [_entry("core", "Python")]
    result = sync_with_market(entries, {"Python": 4}, vacancies_with_stack=10)
    assert result["matched"][0]["market_count"] == 4
    assert result["matched"][0]["market_pct"] == 40.0


def test_sync_unmatched_term_gets_zero():
    entries = [_entry("growing", "Rust")]
    result = sync_with_market(entries, {"Python": 4}, vacancies_with_stack=10)
    assert result["matched"][0]["market_count"] == 0
    assert result["matched"][0]["market_pct"] == 0.0


def test_sync_falls_back_to_term_ru_when_term_en_empty():
    entries = [_entry("core", term_en="", term_ru="python")]
    result = sync_with_market(entries, {"python": 3}, vacancies_with_stack=10)
    assert result["matched"][0]["market_count"] == 3


def test_sync_gap_candidates_exclude_matched_terms():
    entries = [_entry("core", "Python")]
    result = sync_with_market(
        entries, {"Python": 4, "LangChain": 2}, vacancies_with_stack=10
    )
    gap_terms = [g["term"] for g in result["gap_candidates"]]
    assert gap_terms == ["LangChain"]


def test_sync_gap_candidates_sorted_desc_by_count():
    result = sync_with_market(
        [], {"Rare": 1, "Common": 9, "Mid": 5}, vacancies_with_stack=10
    )
    terms = [g["term"] for g in result["gap_candidates"]]
    assert terms == ["Common", "Mid", "Rare"]


def test_sync_no_market_data_all_zero():
    entries = [_entry("core", "Python")]
    result = sync_with_market(entries, {}, vacancies_with_stack=0)
    assert result["matched"][0]["market_count"] == 0
    assert result["gap_candidates"] == []


def test_sync_matched_preserves_input_order():
    entries = [_entry("core", "A"), _entry("growing", "B"), _entry("skip", "C")]
    result = sync_with_market(entries, {}, vacancies_with_stack=1)
    assert [m["term_en"] for m in result["matched"]] == ["A", "B", "C"]
