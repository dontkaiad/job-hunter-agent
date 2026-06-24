"""Tests for the EUR/mo salary floor (FX-converted, sourced from the candidate
PROFILE) + the profile-driven rubric rendered into SCORE_SYSTEM. All LLM and FX
are mocked — no real network. No personal data: assertions use the GENERIC
example profile (config/profile.example.yaml), whose floor is EUR 1000/mo."""

from __future__ import annotations

import pytest

from _personal_needles import personal_needle, personal_needles
from job_hunter import llm, pipeline, scoring, store
from job_hunter.pipeline import Deps
from job_hunter.profile import example_profile
from job_hunter.states import REJECTED, SURFACED

# The example profile's floor (generic placeholder).
EXAMPLE_FLOOR_EUR = example_profile().salary_floor_eur  # 1000

# FakeFx in conftest uses RUB-per-unit: {"RUB":1, "USD":90, "EUR":100}.
# So the example EUR 1000/mo floor = 1000 * 100 = 100_000 RUB.
FLOOR_RUB = EXAMPLE_FLOOR_EUR * 100  # 100_000


def _insert(conn, text, mid="1"):
    return store.insert_item(conn, raw_text=text, source_channel="@c",
                             source_message_id=mid)


# ---------------------------------------------------------------------------
# TASK A: EUR salary floor (from profile), converted via the existing FX layer
# ---------------------------------------------------------------------------


def test_floor_default_is_generic_not_personal():
    """The module-level default floor is a GENERIC placeholder, NOT a real
    candidate figure, and the live floor comes from the profile."""
    assert scoring.DEFAULT_SALARY_FLOOR_EUR == 1000.0
    assert scoring.MIN_SALARY_EUR_PER_MONTH == scoring.DEFAULT_SALARY_FLOOR_EUR
    # The example (committed) profile floor is the generic placeholder.
    assert EXAMPLE_FLOOR_EUR == 1000.0


def test_floor_reason_string_mentions_eur_floor(conn, fake_llm, fake_fx):
    """Hard-reject reason must reference the EUR/mo floor from the profile."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.set_for("hiring-fit JUDGE",
                     '{"relevance_score": 95, "Обоснование": "great"}')
    # 50k RUB top < 100k floor (EUR 1000 * 100).
    item_id = _insert(conn, "Remote AI LLM engineer. Salary 50000 RUB. @hr")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == REJECTED
    trans = store.list_transitions(conn, item_id)
    reasons = " ".join((t["reason"] or "").lower() for t in trans)
    assert "eur 1000" in reasons


def test_below_floor_rejected_despite_high_score(conn, fake_llm, fake_fx):
    """RUB salary below the EUR-floor equivalent -> rejected even if Sonnet scores high."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.set_for("hiring-fit JUDGE",
                     '{"relevance_score": 99, "Обоснование": "идеальная AI-роль"}')
    # 90k RUB < 100k floor (the EUR-1000 equivalent).
    item_id = _insert(conn, "Remote AI Prompt Engineer, RAG. Salary 90000 RUB. @hr")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == REJECTED
    assert item.relevance_score == 99.0  # the high score was stored but overridden


def test_above_floor_surfaced(conn, fake_llm, fake_fx):
    """RUB salary above the EUR-floor equivalent -> kept/surfaced."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.set_for("hiring-fit JUDGE",
                     '{"relevance_score": 85, "Обоснование": "AI-роль, remote"}')
    # 150k RUB > 100k floor.
    item_id = _insert(conn, "Remote AI Prompt Engineer, RAG. Salary 150000 RUB. @hr")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == SURFACED


def test_floor_uses_fx_for_non_eur_currency_below(conn, fake_llm, fake_fx):
    """Prove FX is actually used: a USD posting is converted, then compared.

    1000 USD * 90 RUB/USD = 90_000 RUB, which is below the 100_000 RUB floor
    (EUR 1000 * 100). Must reject -> proves both sides went through FX.
    """
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.set_for("hiring-fit JUDGE",
                     '{"relevance_score": 90, "Обоснование": "good role"}')
    item_id = _insert(conn, "Remote AI LLM engineer. Salary 800-1000 USD/mo. @hr",
                      mid="usd_low")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == REJECTED


def test_floor_uses_fx_for_non_eur_currency_above(conn, fake_llm, fake_fx):
    """USD posting above the floor: 2000 USD * 90 = 180_000 RUB > 100_000 floor."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.set_for("hiring-fit JUDGE",
                     '{"relevance_score": 88, "Обоснование": "good role"}')
    item_id = _insert(conn, "Remote AI LLM engineer. Salary 1500-2000 USD/mo. @hr",
                      mid="usd_high")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == SURFACED


def test_floor_eur_posting_just_below(conn, fake_llm, fake_fx):
    """A EUR posting just below the floor (e.g. 900 vs 1000) -> rejected."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.set_for("hiring-fit JUDGE",
                     '{"relevance_score": 92, "Обоснование": "good role"}')
    item_id = _insert(conn, "Remote AI LLM engineer. Salary 700-900 EUR/mo. @hr",
                      mid="eur_low")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    assert item.state == REJECTED


def test_unstated_salary_not_rejected(conn, fake_llm, fake_fx):
    """Salary UNSTATED -> NOT a hard reject; falls through to the score path."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.set_for("hiring-fit JUDGE",
                     '{"relevance_score": 80, "Обоснование": "AI-роль без зарплаты"}')
    item_id = _insert(conn, "Remote AI Prompt Engineer building RAG and routing. @hr",
                      mid="no_salary")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    # No salary -> guard does not fire; high score surfaces it.
    assert item.state == SURFACED
    trans = store.list_transitions(conn, item_id)
    reasons = " ".join((t["reason"] or "").lower() for t in trans)
    assert "hard reject" not in reasons


def test_floor_not_enforced_without_fx(conn, fake_llm):
    """No FX configured -> floor cannot be computed -> guard does not reject."""
    deps = Deps(llm_client=fake_llm, fx=None, use_llm_extract=False)
    fake_llm.set_for("hiring-fit JUDGE",
                     '{"relevance_score": 75, "Обоснование": "ok"}')
    item_id = _insert(conn, "Remote AI LLM engineer. Salary 50000 RUB. @hr",
                      mid="no_fx")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    # RUB top is known (50k) but with no FX the floor is None -> not a hard
    # reject; the (passing) score surfaces it instead.
    assert item.state == SURFACED


# ---------------------------------------------------------------------------
# TASK B: candidate profile + rubric baked into SCORE_SYSTEM (prompt contract)
# ---------------------------------------------------------------------------


def test_score_system_contains_candidate_profile():
    """The rendered SCORE_SYSTEM injects the GENERIC example profile block (no
    personal data). It must carry the profile scaffolding + the example values."""
    s = " ".join(llm.SCORE_SYSTEM.split())
    assert "CANDIDATE PROFILE" in s
    # Example profile generic (fictional) role text.
    assert "applied-LLM engineer" in s
    assert "model-backed" in s
    assert "seniority levels" in s


def test_score_system_contains_eur_floor_junior_clause():
    """The junior clause references the EUR floor sourced from the (example)
    profile — the generic placeholder floor, not a personal salary figure."""
    s = " ".join(llm.SCORE_SYSTEM.split())
    assert "EUR 1000" in s  # example profile floor
    # No specific personal salary figure must appear in the committed constant.
    assert ("EUR " + personal_needle("salary_floor")) not in s
    assert "junior" in s.lower()


def test_score_system_contains_location_priority():
    s = " ".join(llm.SCORE_SYSTEM.split())
    assert "LOCATION PRIORITY" in s
    assert "relocation" in s.lower()
    assert "remote" in s.lower()
    assert "hybrid" in s.lower()
    # Generic relocation/work-abroad preference from the example profile.
    assert "work abroad" in s.lower()


def test_score_system_has_no_personal_data():
    """Hard guard: the rendered module prompt must contain ZERO personal data."""
    s = llm.SCORE_SYSTEM
    for needle in personal_needles():
        assert needle not in s, "personal data leaked into SCORE_SYSTEM"


def test_draft_system_has_no_personal_data():
    s = llm.DRAFT_SYSTEM
    vector_db = personal_needle("vector_db")  # public OSS name, generic allowlist
    for needle in personal_needles():
        # The vector-DB product name appears in the generic Cyrillic-term
        # allowlist (a public OSS name, legitimately present in DRAFT_SYSTEM via
        # job_hunter/llm.py); everything else must be absent.
        if needle == vector_db:
            continue
        assert needle not in s, "personal data leaked into DRAFT_SYSTEM"


def test_score_system_strong_fit_owns_ai():
    s = " ".join(llm.SCORE_SYSTEM.split())
    assert "STRONG FIT" in s
    assert "OWNS the AI" in s


def test_score_system_flags_not_rejects():
    s = " ".join(llm.SCORE_SYSTEM.split())
    assert "FLAG / DOWN-WEIGHT" in s
    assert "salary unstated" in s.lower()
    assert "do NOT reject" in s


def test_score_system_low_reject_rules_present():
    # Normalize whitespace so wrapped phrases match regardless of line breaks.
    s = " ".join(llm.SCORE_SYSTEM.split()).lower()
    # backend-with-LLM-on-the-side underfit
    assert "backend developer with llm 'on the side'" in s
    # livecoding / hardcore algo
    assert "livecoding from scratch" in s
    assert "hardcore algorithmic interview" in s
    # hard degree requirement
    assert "hard requirement for a technical degree" in s
    # lead/management not a fit
    assert "lead / management -> not a fit" in s


def test_score_system_still_says_ignore_salary_and_json_contract():
    s = llm.SCORE_SYSTEM
    assert "Do NOT consider salary thresholds" in s
    assert "relevance_score" in s
    assert "Обоснование" in s


def test_score_system_evals_in_development_not_handson():
    s = " ".join(llm.SCORE_SYSTEM.split())
    # The example profile's in-development bullet (generic placeholder).
    assert "example in-progress skill" in s
    assert "NOT hands-on yet" in s


# ---------------------------------------------------------------------------
# TASK B: rubric-driven pipeline behavior (mocked Sonnet reflecting the rubric)
# ---------------------------------------------------------------------------

AI_OWNER_POST = (
    "Remote AI / Prompt Engineer. You OWN model behavior: prompts, routing, "
    "production RAG (Qdrant), output control. Salary 350000 RUB. @hr"
)
BACKEND_LLM_SIDE_POST = (
    "Office backend Python developer (Django). Occasionally calls an LLM API on "
    "the side. Salary 350000 RUB. Moscow. @hr"
)
LIVECODING_POST = (
    "Senior engineer. Hardcore algorithmic livecoding interview, write Python "
    "from scratch on a whiteboard. Salary 400000 RUB. @hr"
)
DEGREE_POST = (
    "AI engineer. HARD requirement: a technical university degree in CS. "
    "Salary 350000 RUB. @hr"
)
LEAD_POST = (
    "Engineering Team Lead / Manager. Lead a team of 8, own delivery. "
    "Salary 500000 RUB. @hr"
)


def _run(conn, fake_llm, fake_fx, text, score, mid):
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.set_for("hiring-fit JUDGE",
                     '{"relevance_score": %d, "Обоснование": "r"}' % score)
    item_id = _insert(conn, text, mid=mid)
    pipeline.run_to_gate(conn, item_id, deps=deps)
    return store.get_item(conn, item_id)


def test_ai_owner_surfaces_backend_side_rejected(conn, fake_fx):
    """AI-owning role (rubric STRONG FIT -> high) surfaces; backend-with-LLM-on-
    the-side (rubric LOW underfit) is rejected. Driven by rubric-reflecting
    mocked Sonnet scores."""
    from tests.conftest import FakeLLM

    owner = _run(conn, FakeLLM(), fake_fx, AI_OWNER_POST, 88, "owner")
    backend = _run(conn, FakeLLM(), fake_fx, BACKEND_LLM_SIDE_POST, 30, "backend")
    assert owner.state == SURFACED
    assert backend.state == REJECTED
    assert owner.relevance_score > backend.relevance_score


@pytest.mark.parametrize("text,mid", [
    (LIVECODING_POST, "live"),
    (DEGREE_POST, "degree"),
    (LEAD_POST, "lead"),
])
def test_low_fit_roles_rejected_on_low_score(conn, fake_fx, text, mid):
    """Livecoding-from-scratch, hard-degree, and lead/management roles map to a
    LOW rubric score -> rejected at T3 (salary above floor, so it's the SCORE
    that rejects, matching the rubric)."""
    from tests.conftest import FakeLLM

    item = _run(conn, FakeLLM(), fake_fx, text, 20, mid)
    assert item.state == REJECTED
    assert item.relevance_score < scoring.SURFACE_THRESHOLD
