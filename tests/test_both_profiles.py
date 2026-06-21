"""Both-profiles validation tests (Tester validation pass).

Confirms the core ask: the bot is fully functional with EITHER
  1. Only config/profile.example.yaml present (local absent), OR
  2. config/profile.local.yaml present (preferred over example).

All tests use tmp_path or inline Profile objects — NO personal data
from config/profile.local.yaml is copied into this file.

Coverage added:
  - example-only path: resolve_profile_path returns example when local absent
  - example-only path: pipeline scoring + draft with example floor (1000 EUR)
  - local-preferred path: resolve_profile_path returns local when present
  - local-preferred path: pipeline floor comes from local profile (tmp generic local)
  - neither-file-exists: load_profile raises a clear FileNotFoundError
  - build_score_system with local-style profile injects that profile's floor
  - build_draft_system with local-style profile injects WOMAN + github from it
  - agents.append_draft_signature uses the profile's github/resume (not hardcoded)
  - Deps.profile flows through _salary_floor_rub into salary_guard_reject
"""

from __future__ import annotations

import json
import textwrap

import pytest

from job_hunter import llm, pipeline, store
from job_hunter.pipeline import Deps
from job_hunter.profile import (
    DraftSignature,
    Profile,
    example_profile,
    load_profile,
    load_profile_file,
    resolve_profile_path,
)
from job_hunter.states import REJECTED, SURFACED


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(path, text):
    """Write text to path and return the str path."""
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


def _insert(conn, text, mid="1"):
    return store.insert_item(conn, raw_text=text, source_channel="@c",
                             source_message_id=mid)


# FakeFx from conftest: RUB=1, USD=90, EUR=100
# So floor_rub = salary_floor_eur * 100
FAKE_EUR_RATE = 100.0

# Post texts with explicit salaries.
# With example floor (1000 EUR * 100 = 100k RUB): 90k rejects, 150k surfaces.
# With a higher floor (e.g. 1200 EUR * 100 = 120k RUB): 110k rejects too.
POST_90K_RUB = "Remote AI Prompt Engineer, RAG. Salary 80000-90000 RUB. @hr"
POST_150K_RUB = "Remote AI LLM Engineer. Salary 120000-150000 RUB. @hr"
POST_110K_RUB = "Remote AI LLM Engineer. Salary 100000-110000 RUB. @hr"


def _high_score_deps(fake_llm, fake_fx, profile=None):
    fake_llm.set_for(
        "hiring-fit JUDGE",
        '{"relevance_score": 88, "Обоснование": "strong fit"}',
    )
    if profile is None:
        return Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    return Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False,
                profile=profile)


# ---------------------------------------------------------------------------
# 1. Example-only path: resolve_profile_path falls back to example when local absent
# ---------------------------------------------------------------------------

def test_example_only_resolve_path(tmp_path):
    """When the local profile does not exist, resolve_profile_path must return
    the example path — the bot is fully functional with just the example."""
    example_p = _write(tmp_path / "profile.example.yaml", "role: EXAMPLE\nsalary_floor_eur: 1000\n")
    missing_local = str(tmp_path / "no_local.yaml")
    result = resolve_profile_path(missing_local, example_p)
    assert result == example_p


def test_example_only_load_returns_example_values(tmp_path):
    """load_profile with local absent returns example data — generic floor and
    generic github placeholder."""
    example_p = _write(
        tmp_path / "profile.example.yaml",
        "role: Example role\nsalary_floor_eur: 1000\n"
        "gender: unspecified\n"
        "draft_signature:\n  github: github.com/example\n  resume: '[resume: link]'\n",
    )
    missing = str(tmp_path / "missing.yaml")
    p = load_profile(missing, example_p)
    assert p.salary_floor_eur == 1000.0
    assert p.draft_signature.github == "github.com/example"
    assert p.draft_signature.resume == "[resume: link]"
    assert p.gender == "unspecified"
    assert p.source_path == example_p


# ---------------------------------------------------------------------------
# 2. Example-only path: pipeline scoring + salary guard use example floor
# ---------------------------------------------------------------------------

def test_example_only_pipeline_below_floor_rejected(conn, fake_llm, fake_fx):
    """With only the example profile (floor=1000 EUR), a salary below
    1000 EUR (=100k RUB at 100 RUB/EUR) is hard-rejected even at high score."""
    deps = _high_score_deps(fake_llm, fake_fx)  # Deps defaults to example_profile()
    item_id = _insert(conn, POST_90K_RUB, mid="eo_below")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == REJECTED
    trans = store.list_transitions(conn, item_id)
    reasons = " ".join(t["reason"] or "" for t in trans).lower()
    assert "hard reject" in reasons
    assert "eur 1000" in reasons  # example floor in the reason


def test_example_only_pipeline_above_floor_surfaced(conn, fake_llm, fake_fx):
    """With only the example profile (floor=1000 EUR = 100k RUB), a salary
    above that (150k RUB) surfaces with a good score."""
    deps = _high_score_deps(fake_llm, fake_fx)
    item_id = _insert(conn, POST_150K_RUB, mid="eo_above")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == SURFACED


def test_example_only_draft_signature_is_generic(conn, fake_llm, fake_fx):
    """T11 draft step appends the EXAMPLE profile signature (github.com/example),
    not any personal handle."""
    fake_llm.set_for(
        "hiring-fit JUDGE",
        '{"relevance_score": 88, "Обоснование": "strong fit"}',
    )
    fake_llm.set_for("research", '{"summary":"s","talking_points":[],"questions":[]}')
    fake_llm.set_for("application message", "Здравствуйте, мне интересна роль.")
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    # deps.profile defaults to example_profile()

    item_id = _insert(conn, POST_150K_RUB, mid="eo_sig")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    from job_hunter.states import DECISION_APPROVE
    pipeline.advance_by_id(conn, item_id, decision=DECISION_APPROVE, deps=deps)
    pipeline.advance_by_id(conn, item_id, deps=deps)   # T10 research
    pipeline.advance_by_id(conn, item_id, deps=deps)   # T11 draft

    item = store.get_item(conn, item_id)
    data = json.loads(item.extracted_json)
    draft = data.get("draft", "")
    # The example profile signature is appended by agents.append_draft_signature.
    assert "github.com/example" in draft, (
        "example profile github must appear in draft"
    )
    assert "[resume: link]" in draft, (
        "example profile resume placeholder must appear in draft"
    )
    # No personal github handle in the example-only draft.
    assert ("dont" + "kaiad") not in draft


# ---------------------------------------------------------------------------
# 3. Local-preferred path: resolve_profile_path prefers local when present
# ---------------------------------------------------------------------------

def test_local_preferred_resolve_path(tmp_path):
    """When local profile exists, resolve_profile_path must return it."""
    local_p = _write(tmp_path / "profile.local.yaml", "role: LOCAL\n")
    example_p = _write(tmp_path / "profile.example.yaml", "role: EXAMPLE\n")
    result = resolve_profile_path(local_p, example_p)
    assert result == local_p


def test_local_preferred_load_returns_local_values(tmp_path):
    """load_profile returns LOCAL values (floor, github) when local exists.
    Uses a GENERIC temp local file — no personal data copied here."""
    local_p = _write(
        tmp_path / "profile.local.yaml",
        "role: Custom role\nsalary_floor_eur: 1200\n"
        "gender: female\n"
        "draft_signature:\n  github: github.com/localtest\n  resume: '[cv-local]'\n",
    )
    example_p = _write(
        tmp_path / "profile.example.yaml",
        "role: Generic role\nsalary_floor_eur: 1000\n",
    )
    p = load_profile(local_p, example_p)
    assert p.salary_floor_eur == 1200.0, "local floor must be 1200, not example 1000"
    assert p.draft_signature.github == "github.com/localtest"
    assert p.gender == "female"
    assert p.source_path == local_p


# ---------------------------------------------------------------------------
# 4. Local-preferred path: pipeline uses local profile's floor
# ---------------------------------------------------------------------------

def test_local_profile_higher_floor_rejects_more_aggressively(conn, fake_llm, fake_fx):
    """When Deps.profile has a HIGHER floor than example (e.g. 1200 EUR = 120k RUB),
    a salary of 110k RUB that would SURFACE under the example (100k) floor
    is REJECTED under the local (120k) floor.

    Uses a generic temp Profile — NO real personal data.
    """
    local_profile = Profile(
        role="AI Prompt Engineer",
        salary_floor_eur=1200.0,  # generic test value, not the real personal floor
        gender="unspecified",
        draft_signature=DraftSignature(github="github.com/temptest", resume="[cv]"),
    )
    deps = _high_score_deps(fake_llm, fake_fx, profile=local_profile)
    # 110k RUB top < 120k floor (1200 EUR * 100 RUB/EUR) -> hard reject.
    item_id = _insert(conn, POST_110K_RUB, mid="lp_reject")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == REJECTED
    trans = store.list_transitions(conn, item_id)
    reasons = " ".join(t["reason"] or "" for t in trans).lower()
    assert "hard reject" in reasons
    assert "eur 1200" in reasons  # the local profile's floor in the reason


def test_local_profile_lower_floor_surfaces_more(conn, fake_llm, fake_fx):
    """Symmetric: if the local profile has the SAME floor as example (1000 EUR),
    110k RUB SURFACES (above the 100k floor)."""
    local_profile = Profile(
        salary_floor_eur=1000.0,  # same as example
    )
    deps = _high_score_deps(fake_llm, fake_fx, profile=local_profile)
    item_id = _insert(conn, POST_110K_RUB, mid="lp_surface")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == SURFACED


def test_local_profile_draft_signature_flows_through_pipeline(conn, fake_llm, fake_fx):
    """When Deps.profile has a custom github, agents.draft must append that
    github (not the example placeholder) to the draft text.

    Uses a generic temp Profile — NO real personal data.
    """
    local_profile = Profile(
        salary_floor_eur=1000.0,
        draft_signature=DraftSignature(
            github="github.com/temptest",
            resume="[cv-temptest]",
        ),
        gender="unspecified",
    )
    fake_llm.set_for(
        "hiring-fit JUDGE",
        '{"relevance_score": 88, "Обоснование": "strong fit"}',
    )
    fake_llm.set_for("research", '{"summary":"s","talking_points":[],"questions":[]}')
    fake_llm.set_for("application message", "Отклик на роль.")
    deps = Deps(
        llm_client=fake_llm, fx=fake_fx,
        use_llm_extract=False, profile=local_profile,
    )

    item_id = _insert(conn, POST_150K_RUB, mid="lp_sig")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    from job_hunter.states import DECISION_APPROVE
    pipeline.advance_by_id(conn, item_id, decision=DECISION_APPROVE, deps=deps)
    pipeline.advance_by_id(conn, item_id, deps=deps)   # T10 research
    pipeline.advance_by_id(conn, item_id, deps=deps)   # T11 draft

    item = store.get_item(conn, item_id)
    data = json.loads(item.extracted_json)
    draft = data.get("draft", "")
    assert "github.com/temptest" in draft, (
        "local profile github must appear in draft"
    )
    assert "[cv-temptest]" in draft, (
        "local profile resume placeholder must appear in draft"
    )
    # Example profile placeholder must NOT appear.
    assert "github.com/example" not in draft


# ---------------------------------------------------------------------------
# 5. Local profile female gender -> WOMAN rule in build_draft_system
# ---------------------------------------------------------------------------

def test_local_profile_female_gender_in_score_system(tmp_path):
    """When local profile has gender=female, build_draft_system renders the
    WOMAN (feminine) rule — not UNSPECIFIED from the example profile."""
    local_p = _write(
        tmp_path / "profile.local.yaml",
        "gender: female\nsalary_floor_eur: 1000\n",
    )
    example_p = _write(
        tmp_path / "profile.example.yaml",
        "gender: unspecified\nsalary_floor_eur: 1000\n",
    )
    p = load_profile(local_p, example_p)
    assert p.gender == "female"
    draft_sys = llm.build_draft_system(p)
    assert "WOMAN" in draft_sys
    assert "UNSPECIFIED" not in draft_sys


def test_example_profile_gender_unspecified_in_draft_system():
    """Module-level DRAFT_SYSTEM (from generic example profile) must contain
    UNSPECIFIED (not WOMAN/MAN) since example gender is unspecified."""
    assert "UNSPECIFIED" in llm.DRAFT_SYSTEM
    assert "WOMAN" not in llm.DRAFT_SYSTEM


# ---------------------------------------------------------------------------
# 6. Neither file exists -> clear FileNotFoundError
# ---------------------------------------------------------------------------

def test_neither_profile_exists_raises_file_not_found_error(tmp_path):
    """When both local and example paths are missing, load_profile must raise
    a FileNotFoundError with a message referencing the path — not a silent
    AttributeError or generic crash."""
    missing_local = str(tmp_path / "missing_local.yaml")
    missing_example = str(tmp_path / "missing_example.yaml")
    with pytest.raises(FileNotFoundError):
        load_profile(missing_local, missing_example)


# ---------------------------------------------------------------------------
# 7. build_score_system with custom local profile shows that floor
# ---------------------------------------------------------------------------

def test_build_score_system_shows_local_profile_floor():
    """build_score_system with a Profile(salary_floor_eur=1234) must render
    'EUR 1234' in the profile block — confirming the floor comes from the
    injected profile, not a hardcoded value."""
    p = Profile(salary_floor_eur=1234.0)
    s = llm.build_score_system(p)
    assert "EUR 1234" in s
    assert "EUR 1000" not in s  # not the example default


def test_module_score_system_constant_uses_example_floor():
    """The module-level SCORE_SYSTEM constant (rendered at import time from
    the GENERIC example profile) must show EUR 1000, not any personal floor."""
    assert "EUR 1000" in llm.SCORE_SYSTEM
    # Personal floor (2500) must not appear in the committed constant.
    assert "EUR " + "25" + "00" not in llm.SCORE_SYSTEM


# ---------------------------------------------------------------------------
# 8. Deps.profile flows into _salary_floor_rub correctly
# ---------------------------------------------------------------------------

def test_deps_profile_floor_used_in_pipeline_helper(fake_fx):
    """pipeline._salary_floor_rub must read the floor from deps.profile,
    not a hardcoded constant."""
    from job_hunter.pipeline import _salary_floor_rub

    profile_a = Profile(salary_floor_eur=1000.0)
    profile_b = Profile(salary_floor_eur=3000.0)

    from tests.conftest import FakeFx
    fx = FakeFx()  # EUR rate = 100

    result_a = _salary_floor_rub(Deps(fx=fx, profile=profile_a))
    result_b = _salary_floor_rub(Deps(fx=fx, profile=profile_b))

    assert result_a == pytest.approx(100_000.0), f"expected 100k, got {result_a}"
    assert result_b == pytest.approx(300_000.0), f"expected 300k, got {result_b}"
    # The two results differ, proving the value came from the profile.
    assert result_a != result_b


# ---------------------------------------------------------------------------
# 9. agents.append_draft_signature uses profile.draft_signature (not hardcoded)
# ---------------------------------------------------------------------------

def test_agents_signature_uses_injected_profile():
    """agents.append_draft_signature(text, profile) appends the profile's own
    github/resume — not the module-level example defaults."""
    from job_hunter import agents

    custom = Profile(
        draft_signature=DraftSignature(
            github="github.com/customtest",
            resume="[cv-customtest]",
        )
    )
    result = agents.append_draft_signature("Body text.", custom)
    assert "github.com/customtest" in result
    assert "[cv-customtest]" in result
    # Example defaults must NOT appear when a custom profile is supplied.
    assert "github.com/example" not in result
    assert "[resume: link]" not in result
