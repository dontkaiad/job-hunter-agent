"""Lenient prefilter + score clamp/threshold + salary guard (pure).

The relevance score itself is produced by the Sonnet rubric judge (llm.py /
pipeline.py); this module only owns the deterministic pure pieces.
"""

from job_hunter import scoring
from job_hunter.schema_extract import ExtractResult


def mk(**kw) -> ExtractResult:
    base = dict(title="t", source_channel="@c")
    base.update(kw)
    return ExtractResult(**base)


# --- Lenient prefilter ------------------------------------------------------


def test_prefilter_keeps_normal_vacancy():
    ex = mk(title="AI Engineer")
    raw = "AI Engineer (remote). Building RAG agents with Claude. Apply @hr_acme"
    assert scoring.prefilter(ex, raw).keep is True


def test_prefilter_keeps_borderline_job():
    # A vague marketing-ish post that MENTIONS AI: prefilter must KEEP it (the
    # Sonnet judge decides relevance, not the prefilter).
    ex = mk(title="Marketing manager")
    raw = "Marketing manager who will use AI tools like ChatGPT to write copy."
    assert scoring.prefilter(ex, raw).keep is True


def test_prefilter_drops_empty():
    ex = mk(title="")
    assert scoring.prefilter(ex, "").keep is False
    assert scoring.prefilter(ex, "   \n  ").keep is False


def test_prefilter_drops_too_short_junk():
    ex = mk(title="hi")
    assert scoring.prefilter(ex, "hi 👍").keep is False


def test_prefilter_drops_looking_for_work_post():
    ex = mk(title="Резюме")
    raw = "Ищу работу Python разработчиком, удаленно. #резюме"
    res = scoring.prefilter(ex, raw)
    assert res.keep is False
    assert "looking for work" in (res.reason or "").lower()


def test_prefilter_falls_back_to_title_when_no_raw():
    ex = mk(title="Senior AI Engineer building RAG systems")
    assert scoring.prefilter(ex, None).keep is True


# --- Score clamp ------------------------------------------------------------


def test_clamp_score_range():
    assert scoring.clamp_score(150) == 100
    assert scoring.clamp_score(-5) == 0
    assert scoring.clamp_score(73.6) == 74
    assert scoring.clamp_score(60) == 60


def test_clamp_score_bad_values():
    assert scoring.clamp_score(None) == 0
    assert scoring.clamp_score("nope") == 0


# --- Threshold --------------------------------------------------------------


def test_passes_threshold_boundary():
    assert scoring.passes_threshold(scoring.SURFACE_THRESHOLD) is True
    assert scoring.passes_threshold(scoring.SURFACE_THRESHOLD - 1) is False
    assert scoring.passes_threshold(100) is True
    assert scoring.passes_threshold(0) is False


# --- Hard salary guard (deterministic, overrides LLM) -----------------------


# Pure comparison: both salary top and the EUR floor arrive already converted to
# RUB. Use an illustrative floor value (arbitrary RUB amount) for the comparison.
_FLOOR_RUB = 250_000.0


def test_salary_guard_rejects_below_floor():
    assert scoring.salary_guard_reject(140_000.0, _FLOOR_RUB) is True


def test_salary_guard_keeps_at_floor():
    assert scoring.salary_guard_reject(_FLOOR_RUB, _FLOOR_RUB) is False


def test_salary_guard_unknown_not_rejected():
    assert scoring.salary_guard_reject(None, _FLOOR_RUB) is False


def test_salary_guard_no_floor_not_rejected():
    # FX unavailable -> floor None -> guard cannot fire.
    assert scoring.salary_guard_reject(10_000.0, None) is False


def test_salary_guard_high_pay_kept():
    assert scoring.salary_guard_reject(300_000.0, _FLOOR_RUB) is False
