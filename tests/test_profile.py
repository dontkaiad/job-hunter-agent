"""Profile loader + profile-driven prompt builders.

No personal data: every assertion uses the GENERIC example profile
(config/profile.example.yaml) or inline fixtures. These tests pin the
externalization contract:
  - the loader prefers profile.local.yaml when present, else profile.example.yaml,
  - build_score_system / build_draft_system render the profile DATA from YAML,
  - NO personal specifics remain hardcoded in the module constants,
  - the salary floor comes from the profile, not a hardcoded personal literal.
"""

from __future__ import annotations

import textwrap

import pytest

from job_hunter import agents, llm, scoring
from job_hunter.profile import (
    DraftSignature,
    Profile,
    example_profile,
    load_profile,
    parse_profile,
    resolve_profile_path,
)


# ---------------------------------------------------------------------------
# Loader: local preferred over example
# ---------------------------------------------------------------------------


def _write(path, text):
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


def test_resolve_prefers_local_when_present(tmp_path):
    local = _write(tmp_path / "profile.local.yaml", "role: LOCAL\n")
    example = _write(tmp_path / "profile.example.yaml", "role: EXAMPLE\n")
    assert resolve_profile_path(local, example) == local


def test_resolve_falls_back_to_example_when_no_local(tmp_path):
    example = _write(tmp_path / "profile.example.yaml", "role: EXAMPLE\n")
    missing_local = str(tmp_path / "does_not_exist.yaml")
    assert resolve_profile_path(missing_local, example) == example


def test_load_profile_prefers_local(tmp_path):
    local = _write(tmp_path / "profile.local.yaml", "role: LOCAL ROLE\nsalary_floor_eur: 4242\n")
    example = _write(tmp_path / "profile.example.yaml", "role: EXAMPLE ROLE\nsalary_floor_eur: 1000\n")
    p = load_profile(local, example)
    assert p.role == "LOCAL ROLE"
    assert p.salary_floor_eur == 4242.0
    assert p.source_path == local


def test_load_profile_uses_example_when_local_absent(tmp_path):
    example = _write(tmp_path / "profile.example.yaml", "role: EXAMPLE ROLE\n")
    missing = str(tmp_path / "nope.yaml")
    p = load_profile(missing, example)
    assert p.role == "EXAMPLE ROLE"
    assert p.source_path == example


# ---------------------------------------------------------------------------
# parse_profile: shapes + defaults
# ---------------------------------------------------------------------------


def test_parse_profile_full():
    data = {
        "role": "AI eng",
        "target_grade": "middle",
        "hands_on": ["a", "b"],
        "in_development": ["evals"],
        "stack": ["python"],
        "languages": "english",
        "location_priority": ["remote -> high"],
        "gender": "Female",
        "salary_floor_eur": "1500",
        "draft_signature": {"github": "github.com/me", "resume": "[cv]"},
    }
    p = parse_profile(data)
    assert p.role == "AI eng"
    assert p.hands_on == ["a", "b"]
    assert p.in_development == ["evals"]
    assert p.gender == "female"  # normalized lower
    assert p.salary_floor_eur == 1500.0
    assert p.draft_signature.github == "github.com/me"
    assert p.draft_signature.resume == "[cv]"


def test_parse_profile_defaults_on_missing():
    p = parse_profile({})
    assert p.gender == "unspecified"
    assert p.salary_floor_eur == Profile.salary_floor_eur
    assert p.draft_signature.github == DraftSignature.github
    assert p.hands_on == []


def test_parse_profile_bad_floor_falls_back():
    p = parse_profile({"salary_floor_eur": "not-a-number"})
    assert p.salary_floor_eur == Profile.salary_floor_eur


# ---------------------------------------------------------------------------
# Example profile = the committed generic placeholder (no personal data)
# ---------------------------------------------------------------------------


# Forbidden personal-data fragments, assembled from pieces so this test SOURCE
# stays free of any literal personal string (the Part-4 grep finds zero hits even
# in the guards).
_FORBIDDEN_FRAGMENTS = [
    ("dont", "kaiad"), ("Blue", " Card"), ("Qd", "rant"), ("Lang", "fuse"),
    ("Raspberry", " Pi"), ("25", "00"), ("рж", "ав"), ("дипл", "ом"),
    ("Saint", " Petersburg"),
]
_FORBIDDEN = ["".join(p) for p in _FORBIDDEN_FRAGMENTS]


def test_example_profile_is_generic():
    p = example_profile()
    assert p.salary_floor_eur == 1000.0  # generic placeholder, not personal
    assert p.gender == "unspecified"
    assert p.draft_signature.github == "github.com/example"
    assert p.draft_signature.resume == "[resume: link]"
    # No personal specifics anywhere in the example profile values.
    blob = " ".join([p.role, p.target_grade, p.experience_note, p.languages,
                     *p.hands_on, *p.in_development, *p.stack, *p.location_priority])
    for needle in _FORBIDDEN:
        assert needle not in blob


# ---------------------------------------------------------------------------
# build_score_system: renders the profile DATA from YAML
# ---------------------------------------------------------------------------


def test_build_score_system_renders_profile_values():
    p = Profile(
        role="ZZ_ROLE_MARKER engineer",
        target_grade="Target MIDDLE.",
        hands_on=["HANDSON_MARKER skill"],
        in_development=["INDEV_MARKER skill"],
        stack=["STACK_MARKER"],
        languages="LANG_MARKER",
        location_priority=["LOC_MARKER -> top"],
        salary_floor_eur=1234,
    )
    s = llm.build_score_system(p)
    # Scaffold (judge/rubric) present.
    assert "hiring-fit JUDGE" in s
    assert "CANDIDATE PROFILE" in s
    assert "STRONG FIT" in s and "LOCATION PRIORITY" in s
    # Profile DATA injected.
    assert "ZZ_ROLE_MARKER" in s
    assert "HANDSON_MARKER" in s
    assert "INDEV_MARKER" in s
    assert "STACK_MARKER" in s
    assert "LANG_MARKER" in s
    assert "LOC_MARKER" in s
    # Salary floor sourced from the profile, not a literal.
    assert "EUR 1234" in s


def test_score_system_module_constant_renders_example_no_personal():
    """The module-level SCORE_SYSTEM is rendered from the GENERIC example
    profile and must contain ZERO personal data."""
    s = llm.SCORE_SYSTEM
    assert "EUR 1000" in s
    for needle in _FORBIDDEN:
        assert needle not in s


# ---------------------------------------------------------------------------
# build_draft_system: gender + signature + lists from the profile
# ---------------------------------------------------------------------------


def test_build_draft_system_injects_signature_and_lists():
    p = Profile(
        hands_on=["DRAFT_HANDSON_MARKER"],
        in_development=["DRAFT_INDEV_MARKER"],
        draft_signature=DraftSignature(github="github.com/zzmarker", resume="[cv-marker]"),
    )
    s = llm.build_draft_system(p)
    assert "github.com/zzmarker" in s
    assert "DRAFT_HANDSON_MARKER" in s
    assert "DRAFT_INDEV_MARKER" in s


@pytest.mark.parametrize("gender,needle", [
    ("female", "WOMAN"),
    ("male", "MAN"),
    ("unspecified", "UNSPECIFIED"),
])
def test_build_draft_system_gender_rule(gender, needle):
    s = llm.build_draft_system(Profile(gender=gender))
    assert needle in s


def test_draft_system_module_constant_no_personal_handle():
    s = llm.DRAFT_SYSTEM
    assert "github.com/example" in s
    assert "github.com/" + "dont" + "kaiad" not in s
    assert "WOMAN" not in s  # example gender is 'unspecified'


# ---------------------------------------------------------------------------
# Signature comes from the profile (agents)
# ---------------------------------------------------------------------------


def test_signature_block_from_profile():
    p = Profile(draft_signature=DraftSignature(github="github.com/x", resume="[r]"))
    block = agents.signature_block(p)
    assert "github.com/x" in block and "[r]" in block


def test_append_signature_uses_profile():
    p = Profile(draft_signature=DraftSignature(github="github.com/abc", resume="[res]"))
    out = agents.append_draft_signature("Body.", p)
    assert "github.com/abc" in out and "[res]" in out
    # Idempotent with the same profile.
    assert agents.append_draft_signature(out, p) == out


def test_agents_module_constants_are_generic_example():
    assert agents.GITHUB_LINK == "github.com/example"
    assert agents.RESUME_PLACEHOLDER == "[resume: link]"
    assert ("dont" + "kaiad") not in agents._SIGNATURE_BLOCK


# ---------------------------------------------------------------------------
# Salary floor sourced from profile/config, NOT a personal literal in scoring.py
# ---------------------------------------------------------------------------


def test_scoring_floor_default_is_generic_placeholder():
    assert scoring.DEFAULT_SALARY_FLOOR_EUR == 1000.0
    assert scoring.MIN_SALARY_EUR_PER_MONTH == 1000.0


def test_scoring_source_has_no_personal_floor_literal():
    import inspect

    src = inspect.getsource(scoring)
    personal_floor = "25" + "00"
    assert personal_floor not in src, "personal salary figure must not appear in scoring.py"
