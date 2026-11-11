"""SALARY_GUARD_ENABLED: the env switch that skips ONLY the hard salary-floor reject.

Default (unset): the guard works exactly as before. 0/false/no/off: a below-floor salary is not rejected, and
nothing else changes (the profile floor and the prompts that quote it stay as they are).
"""

from __future__ import annotations

import pytest

from job_hunter import llm, pipeline, store
from job_hunter.config import load_config
from job_hunter.pipeline import Deps
from job_hunter.profile import example_profile
from job_hunter.states import REJECTED, SURFACED

# FakeFx (conftest): EUR=100 RUB, so the example floor (EUR 1000) is 100k RUB.
POST_BELOW_FLOOR = "Remote AI Prompt Engineer, RAG. Salary 80000-90000 RUB. @hr"


def _deps(fake_llm, fake_fx, **kw):
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 88, "Обоснование": "strong fit"}')
    return Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False, **kw)


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

