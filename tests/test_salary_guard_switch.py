"""SALARY_GUARD_ENABLED: the env switch that skips ONLY the hard salary-floor reject.

Default (unset): the guard works exactly as before. 0/false/no/off: a below-floor salary is not rejected, and
nothing else changes (the profile floor and the prompts that quote it stay as they are).
"""

from __future__ import annotations

import pytest

from _llm_extract import llm_extracts_salary_of
from job_hunter import llm, pipeline, store
from job_hunter.config import load_config
from job_hunter.pipeline import Deps
from job_hunter.profile import example_profile
from job_hunter.states import REJECTED, SURFACED

# FakeFx (conftest): EUR=100 RUB, so the example floor (EUR 1000) is 100k RUB.
POST_BELOW_FLOOR = "Remote AI Prompt Engineer, RAG. Salary 80000-90000 RUB. @hr"


def _deps(fake_llm, fake_fx, **kw):
    """LLM extraction (the row is LLM-extracted): since B1 the hard salary guard ignores heuristic rows."""
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 88, "Обоснование": "strong fit"}')
    llm_extracts_salary_of(fake_llm, POST_BELOW_FLOOR)
    return Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=True, **kw)


def _run(conn, deps, mid):
    item_id = store.insert_item(conn, raw_text=POST_BELOW_FLOOR, source_channel="@c", source_message_id=mid)
    pipeline.run_to_gate(conn, item_id, deps=deps)
    return store.get_item(conn, item_id), store.list_transitions(conn, item_id)


# --- config parsing --------------------------------------------------------


def test_config_default_is_guard_on():
    assert load_config({}).salary_guard is True
    assert load_config({"SALARY_GUARD_ENABLED": ""}).salary_guard is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "Off"])
def test_config_off_values(value):
    assert load_config({"SALARY_GUARD_ENABLED": value}).salary_guard is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_config_on_values(value):
    assert load_config({"SALARY_GUARD_ENABLED": value}).salary_guard is True


def test_deps_default_is_guard_on():
    assert Deps().salary_guard is True


# --- pipeline: both modes --------------------------------------------------


def test_default_guard_rejects_below_floor(conn, fake_llm, fake_fx):
    item, trans = _run(conn, _deps(fake_llm, fake_fx), "sg_default")
    assert item.state == REJECTED
    assert any("hard reject: salary" in (t["reason"] or "") for t in trans)


def test_guard_off_does_not_reject_below_floor(conn, fake_llm, fake_fx):
    item, trans = _run(conn, _deps(fake_llm, fake_fx, salary_guard=False), "sg_off")
    assert item.state != REJECTED
    assert item.state == SURFACED
    assert not any("hard reject: salary" in (t["reason"] or "") for t in trans)


def test_guard_off_leaves_floor_and_prompt_untouched():
    """The switch must not touch the profile floor or the text of the scoring prompt."""
    profile = example_profile()
    floor = int(round(profile.salary_floor_eur))
    on, off = Deps(profile=profile), Deps(profile=profile, salary_guard=False)
    assert on.profile.salary_floor_eur == off.profile.salary_floor_eur
    assert llm.render_profile_block(on.profile) == llm.render_profile_block(off.profile)
    assert f">= EUR {floor}/month" in llm.render_profile_block(off.profile)


# --- B1: the hard salary guard acts only on LLM-extracted rows ---------------------------------------------


def _heuristic_run(conn, fake_llm, fake_fx, mid, **kw):
    """The same post and the same below-floor salary, but the row comes from the heuristic extractor."""
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 88, "Обоснование": "strong fit"}')
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False, **kw)
    return _run(conn, deps, mid)


def test_heuristic_row_with_a_below_floor_salary_is_not_rejected(conn, fake_llm, fake_fx):
    item, trans = _heuristic_run(conn, fake_llm, fake_fx, "sg_heur")
    assert item.extract_mode == "heuristic:no-llm"
    assert item.state == SURFACED                                          # the same row the LLM path rejects
    assert not any("hard reject: salary" in (t["reason"] or "") for t in trans)


def test_llm_row_with_the_same_salary_is_still_rejected(conn, fake_llm, fake_fx):
    item, trans = _run(conn, _deps(fake_llm, fake_fx), "sg_llm")
    assert item.extract_mode == "llm"
    assert item.state == REJECTED
    assert any("hard reject: salary" in (t["reason"] or "") for t in trans)


def test_the_heuristic_exemption_does_not_touch_the_other_guards(conn, fake_llm, fake_fx):
    """Only the salary reject is skipped for a heuristic row: an office-only post is still rejected by the location guard."""
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 90, "Обоснование": "fit"}')
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    item_id = store.insert_item(conn, raw_text="Python LLM engineer #office Moscow. Salary 80000-90000 RUB. @hr",
                                source_channel="@c", source_message_id="sg_loc")
    pipeline.run_to_gate(conn, item_id, deps=deps)
    item = store.get_item(conn, item_id)
    trans = store.list_transitions(conn, item_id)
    assert item.extract_mode == "heuristic:no-llm"
    assert not any("hard reject: salary" in (t["reason"] or "") for t in trans)
    assert item.state == REJECTED                                          # ...but the location guard still acts on the same row
    assert any("физически недоступно" in (t["reason"] or "") for t in trans)


@pytest.mark.parametrize("mode,applies", [
    ("llm", True), (None, True), ("", True),                                # LLM extraction and an unknown (old) row: as before
    ("heuristic:no-llm", False), ("heuristic:prefilter", False), ("heuristic:duplicate", False),
    ("heuristic:topic", False), ("heuristic:fallback", False), ("heuristic", False),
])
def test_guard_applies_by_extract_mode(mode, applies):
    class Item:                                                             # only the field the helper reads
        extract_mode = mode
    assert pipeline._salary_guard_applies(Item()) is applies


def test_guard_off_still_wins_over_the_mode(conn, fake_llm, fake_fx):
    """The env switch is independent of the mode: off means no salary reject for an LLM row either."""
    item, _ = _run(conn, _deps(fake_llm, fake_fx, salary_guard=False), "sg_llm_off")
    assert item.extract_mode == "llm" and item.state == SURFACED

