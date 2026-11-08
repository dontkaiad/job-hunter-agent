"""The golden-set machinery: strata, deterministic sample, label classes, estimators and
the blind card. Pure -- no DB, no API."""
import json

import pytest

from job_hunter import golden as g
from evals import golden_eval as ge


def _frame(sizes, hard=136):
    fr, i = [], 1
    for b, n in sizes.items():
        for _ in range(n):
            fr.append({"item_id": i, "band": b, "stratum": "core"}); i += 1
    for _ in range(hard):
        fr.append({"item_id": i, "band": "<25", "stratum": "hard"}); i += 1
    return fr


BIG = {"<25": 700, "25-39": 500, "40-49": 150, "50-59": 80, "60-69": 60, "70-79": 70, "80+": 20}


# --- bands / allocation -----------------------------------------------------

@pytest.mark.parametrize("score,band", [(None, "<25"), (0, "<25"), (24.9, "<25"), (25, "25-39"), (39, "25-39"),
                                         (40, "40-49"), (59, "50-59"), (60, "60-69"), (69, "60-69"),
                                         (70, "70-79"), (80, "80+"), (100, "80+")])
def test_band_edges(score, band):
    assert g.band_of(score) == band


def test_allocation_matches_the_plan_and_caps_at_census():
    assert sum(g.allocate(BIG).values()) == 120
    small = dict(BIG, **{"80+": 5, "60-69": 8})
    n = g.allocate(small)
    assert n["80+"] == 5 and n["60-69"] == 8          # a small band is taken whole
    assert sum(n.values()) == 120                      # the shortfall goes to the big bands
    assert all(n[b] <= small[b] for b in n)
    tiny = {b: 3 for b in g.BAND_NAMES}
    assert g.allocate(tiny) == tiny                    # never more than exists


# --- sample -----------------------------------------------------------------

def test_sample_shape_waves_controls_and_holdout():
    s = g.build_sample(_frame(BIG))
    plain = [r for r in s if not r["is_control"]]
    assert len(s) == 145 and len(plain) == 130
    assert sorted((w, sum(1 for r in s if r["wave"] == w)) for w in (1, 2)) == [(1, 70), (2, 75)]
    assert sum(r["stratum"] == "core" for r in plain) == 120 and sum(r["stratum"] == "hard" for r in plain) == 10
    ctrl = [r for r in s if r["is_control"]]
    assert len(ctrl) == 15 and all(not r["held_out"] for r in ctrl)
    for c in ctrl:
        orig = next(r for r in s if r["wave"] == c["wave"] and r["item_id"] == c["control_of_item"] and not r["is_control"])
        assert c["order_no"] - orig["order_no"] >= g.CONTROL_MIN_GAP
    # order numbers are 1..n per wave without gaps (the UNIQUE (wave, order_no) key)
    for w in (1, 2):
        assert sorted(r["order_no"] for r in s if r["wave"] == w) == list(range(1, sum(1 for r in s if r["wave"] == w) + 1))
    # wave 1 alone is itself stratified across every band
    assert {r["band"] for r in s if r["wave"] == 1 and r["stratum"] == "core" and not r["is_control"]} == set(g.BAND_NAMES)


def test_sample_is_deterministic_and_independent_of_row_order():
    fr = _frame(BIG)
    assert g.build_sample(fr) == g.build_sample(list(reversed(fr)))


def test_holdout_share_and_stability():
    ids = range(1, 20001)
    share = sum(g.held_out(i) for i in ids) / 20000
    assert 0.28 < share < 0.32
    assert [g.held_out(i) for i in range(100)] == [g.held_out(i) for i in range(100)]


def test_a_small_frame_does_not_crash():
    s = g.build_sample(_frame({"<25": 4, "25-39": 4, "40-49": 2, "50-59": 1, "60-69": 3, "70-79": 2, "80+": 1}, hard=3))
    assert len(s) >= 20 and {r["wave"] for r in s} <= {1, 2}


# --- labels -----------------------------------------------------------------

@pytest.mark.parametrize("label,chips,cls", [
    ("yes", [], "relevant"), ("yes", ["fit"], "relevant"),
    ("no", [], "irrelevant"), ("no", ["fit"], "irrelevant"), ("no", ["seniority", "stale"], "irrelevant"),
    ("no", ["stale"], "excluded"), ("no", ["geo"], "excluded"), ("no", ["pay"], "excluded"),
    ("no", ["stale", "geo", "pay"], "excluded"),
    ("no", ["other"], "other"), ("no", ["stale", "other"], "other"),
    ("unsure", [], "unsure"),
])
def test_label_classes(label, chips, cls):
    assert g.classify(label, chips) == cls


# --- estimators -------------------------------------------------------------

def test_estimate_arithmetic_on_a_toy_table():
    sizes = {b: 0 for b in g.BAND_NAMES}
    sizes.update({"<25": 100, "60-69": 10, "80+": 10})
    labels = ([{"band": "<25", "cls": "relevant"}] * 1 + [{"band": "<25", "cls": "irrelevant"}] * 9 +
              [{"band": "60-69", "cls": "relevant"}] * 5 + [{"band": "60-69", "cls": "irrelevant"}] * 5 +
              [{"band": "80+", "cls": "relevant"}] * 8 + [{"band": "80+", "cls": "irrelevant"}] * 2 +
              [{"band": "80+", "cls": "excluded"}] * 4 + [{"band": "80+", "cls": "unsure"}])
    e = g.estimate(labels, sizes)
    # surfaced: 10*0.5 + 10*0.8 = 13 relevant of 20 -> 0.65 ; missed: 100*0.1 = 10
    assert e["precision_at_60"] == pytest.approx(13 / 20)
    assert e["recall_at_60"] == pytest.approx(13 / 23)
    assert e["per_band"]["80+"]["decided"] == 10          # excluded / unsure are not decided
    sens = g.estimate(labels, sizes, sensitivity=True)     # they count as negatives instead
    assert sens["per_band"]["80+"]["decided"] == 15 and sens["precision_at_60"] < e["precision_at_60"]


def test_interval_keeps_uncertainty_for_a_band_with_no_positives():
    sizes = {b: 0 for b in g.BAND_NAMES}
    sizes.update({"<25": 700, "70-79": 50})
    labels = [{"band": "<25", "cls": "irrelevant"}] * 12 + [{"band": "70-79", "cls": "relevant"}] * 10 + \
             [{"band": "70-79", "cls": "irrelevant"}] * 10
    lo, hi = g.interval(labels, sizes, reps=1500)["recall_at_60"]
    assert g.estimate(labels, sizes)["recall_at_60"] == 1.0     # point estimate: nothing missed
    assert lo < 0.9 and hi == pytest.approx(1.0, abs=0.02)      # but the interval says it may be well below


def test_wilson_and_pr_curve():
    lo, hi = g.wilson(0, 10)
    assert lo == 0.0 and 0.2 < hi < 0.35
    labels = [{"band": "70-79", "cls": "relevant", "score": 75}, {"band": "70-79", "cls": "irrelevant", "score": 72},
              {"band": "25-39", "cls": "relevant", "score": 30}, {"band": "25-39", "cls": "irrelevant", "score": 31}]
    pr = {r["threshold"]: r for r in g.pr_curve(labels, {"70-79": 2, "25-39": 4})}
    assert pr[60]["precision"] == pytest.approx(0.5) and pr[60]["recall"] == pytest.approx(1 / 3)
    assert pr[25]["recall"] == pytest.approx(1.0)


# --- blind card -------------------------------------------------------------

def test_card_is_blind():
    ex = {"title": "AI Eng", "company": "Acme", "relevance_score": 91.0, "Обоснование": "secret verdict",
          "source_channel": "@some_channel", "reasons": ["r"], "salary_min": 3000, "salary_max": 4000, "currency": "EUR",
          "stack": ["python"]}
    c = g.card_payload(sample_id=7, position=3, total=70, wave=1, extracted=ex, text="word " * 400,
                       link="https://acme.io/jobs/1", label="no", chips=["fit"])
    assert set(c) == set(g.CARD_FIELDS)
    blob = json.dumps(c, ensure_ascii=False)
    for leak in ("91", "secret verdict", "some_channel", "relevance", "Обоснование", "item_id"):
        assert leak not in blob
    assert c["salary"] == "3000–4000 EUR"
    assert c["snippet"] == ("word " * 400).strip()                    # the whole text, not a 600-char cut


# --- frame rules (the same function the live run uses) ----------------------

def test_offline_eval_passes():
    res = ge.run_offline()
    assert all(v for v in res.values() if isinstance(v, bool)), res


def test_band_source_travels_with_every_card_and_control():
    fr = _frame(BIG)
    for i, r in enumerate(fr):
        r["band_source"] = "current" if i % 4 == 0 else "stored"
    by = {r["item_id"]: r["band_source"] for r in fr}
    s = g.build_sample(fr)
    assert all(r["band_source"] == by[r["item_id"]] for r in s)
    assert any(r["is_control"] for r in s)           # controls keep their original's source


# --- the card shows the whole, readable text -----------------------------------

def test_card_text_is_never_cut_and_keeps_its_structure():
    text = "About the role\r\n\r\n\r\n\r\n- Python  \n- RAG\u00a0pipelines\n\nRequirements:\n" + ("line of requirements. " * 2000)
    out = g.readable_text(text)
    assert out.startswith("About the role\n\n- Python\n- RAG pipelines\n\nRequirements:")
    assert "\r" not in out and "\n\n\n" not in out and "  \n" not in out
    assert out.endswith("line of requirements.") and len(out) > 40_000       # nothing lost
    card = g.card_payload(sample_id=1, position=1, total=1, wave=1, extracted={}, text=text, link=None)
    assert card["snippet"] == out


def test_only_a_runaway_text_is_cut_and_says_so():
    out = g.readable_text("x" * (g.MAX_CARD_TEXT + 5000))
    assert out.endswith("знаков]") and len(out) < g.MAX_CARD_TEXT + 200
    assert g.readable_text(None) == "" and g.readable_text("  \n\n ") == ""


# --- wave 2 as re-planned after wave 1 -------------------------------------------

def _frame2():
    fr, i = [], 1
    sizes = {"<25": 98, "25-39": 1193, "40-49": 131, "50-59": 48, "60-69": 12, "70-79": 123, "80+": 19}
    for b, n in sizes.items():
        for _ in range(n):
            fr.append({"item_id": i, "band": b, "stratum": "core", "band_source": "stored"}); i += 1
    for _ in range(136):
        fr.append({"item_id": i, "band": "<25", "stratum": "hard", "band_source": "current"}); i += 1
    return fr, sizes


def test_wave2_plan_matches_what_was_agreed():
    assert g.WAVE2_PLAN == {"<25": 4, "25-39": 14, "40-49": 6, "50-59": 30, "60-69": 6}
    assert sum(g.WAVE2_PLAN.values()) == 60 and g.WAVE2_CONTROLS == 5


def test_wave2_composition_exclusions_and_order():
    fr, sizes = _frame2()
    wave1 = g.build_sample(fr)
    taken = {r["item_id"] for r in wave1 if r["wave"] == 1}
    w2 = g.build_wave2(fr, taken)
    plain = [r for r in w2 if not r["is_control"]]
    assert len(w2) == 65 and len(plain) == 60 and sum(r["is_control"] for r in w2) == 5
    assert all(r["wave"] == 2 and r["stratum"] == "core" for r in w2)              # no hard stratum
    assert dict(__import__("collections").Counter(r["band"] for r in plain)) == g.WAVE2_PLAN
    assert not ({r["item_id"] for r in w2} & taken)                                # nothing seen in wave 1
    assert sorted(r["order_no"] for r in w2) == list(range(1, 66))
    for c in (r for r in w2 if r["is_control"]):
        first = next(r for r in w2 if r["item_id"] == c["control_of_item"] and not r["is_control"])
        assert c["order_no"] - first["order_no"] >= g.CONTROL_MIN_GAP and not c["held_out"]
    assert w2 == g.build_wave2(list(reversed(fr)), taken)                          # deterministic
    assert all(r["band_source"] == "stored" for r in plain)                        # source travels


def test_wave2_takes_a_small_band_whole_and_never_more_than_exists():
    fr, _ = _frame2()
    sizes_small = {"<25": 2, "25-39": 40, "40-49": 40, "50-59": 33, "60-69": 3}
    fr2 = [r for r in fr if r["stratum"] == "core"]
    keep = []
    for b, n in sizes_small.items():
        keep += [r for r in fr2 if r["band"] == b][:n]
    w2 = g.build_wave2(keep, taken=set())
    c = __import__("collections").Counter(r["band"] for r in w2 if not r["is_control"])
    assert c["<25"] == 2 and c["60-69"] == 3 and c["50-59"] == 30


# --- the offline eval covers the new plan -------------------------------------------

def test_offline_eval_checks_wave2():
    res = ge.run_offline()
    assert res["wave2_matches_the_plan"] is True
