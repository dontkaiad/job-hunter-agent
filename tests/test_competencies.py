"""Tests for job_hunter.competencies — pure parse + market sync, no DB/API."""

from job_hunter.competencies import (
    BUCKETS,
    CompetencyEntry,
    Evidence,
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


def test_parse_term_ru_only_is_kept():
    data = {"core": [{"term_ru": "Только по-русски"}]}
    entries = parse_competencies(data)
    assert len(entries) == 1
    assert entries[0].term_ru == "Только по-русски"
    assert entries[0].term_en == ""


# ---------------------------------------------------------------------------
# evidence parsing — the term-first, multi-project shape
# ---------------------------------------------------------------------------


def test_parse_evidence_list_under_a_term():
    data = {
        "core": [
            {
                "term_en": "Python",
                "evidence": [
                    {"project": "job-hunter-agent", "source_ref": "requirements.txt"},
                    {"project": "Nexus", "source_ref": "pyproject.toml", "note": "backend"},
                ],
            }
        ]
    }
    entries = parse_competencies(data)
    assert len(entries) == 1
    ev = entries[0].evidence
    assert ev == (
        Evidence(project="job-hunter-agent", source_ref="requirements.txt", note=""),
        Evidence(project="Nexus", source_ref="pyproject.toml", note="backend"),
    )


def test_parse_evidence_missing_defaults_to_empty_tuple():
    data = {"core": [{"term_en": "Python"}]}
    entries = parse_competencies(data)
    assert entries[0].evidence == ()


def test_parse_evidence_skips_rows_missing_both_project_and_source_ref():
    data = {
        "core": [
            {
                "term_en": "Python",
                "evidence": [{"note": "no project or source_ref"}, {"project": "X"}],
            }
        ]
    }
    entries = parse_competencies(data)
    assert len(entries[0].evidence) == 1
    assert entries[0].evidence[0].project == "X"


def test_parse_evidence_non_list_is_ignored():
    data = {"core": [{"term_en": "Python", "evidence": "not a list"}]}
    entries = parse_competencies(data)
    assert entries[0].evidence == ()


def test_parse_fills_all_scalar_fields():
    data = {
        "core": [
            {
                "term_ru": "Термин",
                "term_en": "Term",
                "explainer_ru": "30 сек",
                "explainer_en": "30 sec",
                "resume_line": "Did X",
                "evidence": [{"project": "job-hunter-agent", "source_ref": "job_hunter/foo.py:1 (bar)"}],
            }
        ]
    }
    e = parse_competencies(data)[0]
    assert e.bucket == "core"
    assert e.term_ru == "Термин"
    assert e.term_en == "Term"
    assert e.explainer_ru == "30 сек"
    assert e.explainer_en == "30 sec"
    assert e.resume_line == "Did X"
    assert e.evidence == (
        Evidence(project="job-hunter-agent", source_ref="job_hunter/foo.py:1 (bar)", note=""),
    )


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


def test_sync_serializes_evidence_list():
    entries = [
        CompetencyEntry(
            bucket="core",
            term_ru="",
            term_en="Python",
            evidence=(
                Evidence(project="job-hunter-agent", source_ref="requirements.txt"),
                Evidence(project="Nexus", source_ref="pyproject.toml", note="backend"),
            ),
        )
    ]
    result = sync_with_market(entries, {"Python": 2}, vacancies_with_stack=4)
    ev = result["matched"][0]["evidence"]
    assert ev == [
        {"project": "job-hunter-agent", "source_ref": "requirements.txt", "note": ""},
        {"project": "Nexus", "source_ref": "pyproject.toml", "note": "backend"},
    ]
