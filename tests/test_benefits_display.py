"""DISPLAY-ONLY benefits/conditions field.

Covers: heuristic extraction from a «Что мы предлагаем» block, the card field
(present when populated / absent when empty), schema round-trip + back-compat
default, the LLM extract path (prompt instructs benefits + parse passes through),
and — critically — PROOF that benefits have ZERO scoring impact.
"""

import json

from job_hunter import bot, extract, llm, store
from job_hunter.schema_extract import (
    ExtractResult, from_dict, serialize, validate,
)


# --- Heuristic extraction ---------------------------------------------------


def test_extract_benefits_from_what_we_offer_block_russian():
    raw = (
        "Senior AI Engineer\n"
        "Что мы предлагаем:\n"
        "- ДМС\n"
        "- обучение за счёт компании\n"
        "- помощь с релокацией\n"
        "- оплачиваемый отпуск\n"
    )
    res = extract.extract(raw, source_channel="@c")
    # RU labels (post is Russian).
    assert "ДМС / медстраховка" in res.benefits
    assert "Обучение" in res.benefits
    assert "Помощь с релокацией" in res.benefits
    assert "Оплачиваемый отпуск" in res.benefits


def test_extract_benefits_english_labels_for_english_post():
    raw = (
        "AI Engineer\n"
        "We offer: health insurance, learning budget, visa sponsorship, "
        "equipment, paid vacation.\n"
    )
    res = extract.extract(raw, source_channel="@c")
    assert "Health insurance" in res.benefits
    assert "Learning budget" in res.benefits
    assert "Visa sponsorship" in res.benefits


def test_extract_benefits_dedup_and_default_empty():
    # Same benefit triggered twice -> appears once.
    raw = "Предлагаем ДМС и медстраховку для всех."
    res = extract.extract(raw, source_channel="@c")
    assert res.benefits.count("ДМС / медстраховка") == 1
    # A post with no perks -> empty list (always present).
    bare = extract.extract("Просто вакансия без условий.", source_channel="@c")
    assert bare.benefits == []


def test_extract_benefits_always_present_list():
    res = extract.extract("anything", source_channel="@c")
    assert isinstance(res.benefits, list)


# --- Card rendering ---------------------------------------------------------


def _surfaced_item(conn, ex: dict, score: float = 80.0):
    item_id = store.insert_item(
        conn, raw_text=ex.get("_raw", "x"), source_channel="@c", source_message_id="1"
    )
    payload = {k: v for k, v in ex.items() if not k.startswith("_")}
    store.update_state(
        conn, item_id, "extracted", from_state="discovered",
        kind="deterministic", actor="system",
        extracted_json=json.dumps(payload), relevance_score=score,
    )
    return store.get_item(conn, item_id)


def test_card_renders_benefits_russian(conn):
    ex = {
        "title": "AI Eng", "stack": ["python"], "remote": True,
        "benefits": ["ДМС / медстраховка", "Обучение", "Помощь с релокацией"],
        "_raw": "Вакансия с условиями: ДМС, обучение.",
    }
    item = _surfaced_item(conn, ex)
    text = bot.render_surfaced(item)
    assert "<b>Условия</b>:" in text
    assert "ДМС / медстраховка, Обучение, Помощь с релокацией" in text


def test_card_renders_benefits_english(conn):
    ex = {
        "title": "AI Eng", "stack": ["python"], "remote": True,
        "benefits": ["Health insurance", "Learning budget"],
        "_raw": "A job with health insurance and learning budget.",
    }
    item = _surfaced_item(conn, ex)
    text = bot.render_surfaced(item)
    assert "<b>Perks</b>:" in text
    assert "Health insurance, Learning budget" in text


def test_card_omits_benefits_field_when_empty(conn):
    ex = {
        "title": "AI Eng", "stack": ["python"], "remote": True, "benefits": [],
        "_raw": "Вакансия без условий.",
    }
    item = _surfaced_item(conn, ex)
    text = bot.render_surfaced(item)
    assert "Условия" not in text
    assert "Perks" not in text


def test_card_benefits_html_escaped(conn):
    ex = {
        "title": "AI Eng", "remote": True,
        "benefits": ["ДМС <b>&</b> спорт"],
        "_raw": "Вакансия.",
    }
    item = _surfaced_item(conn, ex)
    text = bot.render_surfaced(item)
    # The label is bold (our constant), the value is escaped.
    assert "ДМС &lt;b&gt;&amp;&lt;/b&gt; спорт" in text


# --- Schema round-trip + back-compat ---------------------------------------


def test_schema_roundtrip_preserves_benefits():
    res = ExtractResult(
        title="t", source_channel="@c", benefits=["ДМС / медстраховка", "Обучение"]
    )
    again = from_dict(json.loads(serialize(res)))
    assert again.benefits == ["ДМС / медстраховка", "Обучение"]


def test_from_dict_defaults_benefits_when_missing():
    # An OLD extracted_json row with no benefits key still loads.
    old = {"title": "t", "source_channel": "@c", "stack": ["python"]}
    res = from_dict(old)
    assert res.benefits == []


def test_from_dict_coerces_benefits_string_to_list():
    res = from_dict({"title": "t", "source_channel": "@c", "benefits": "ДМС"})
    assert res.benefits == ["ДМС"]


def test_validate_flags_non_string_benefits():
    res = ExtractResult(title="t", source_channel="@c")
    res.benefits = [123]  # type: ignore[list-item]
    assert any("benefits" in w for w in validate(res))


# --- LLM extract path -------------------------------------------------------


def test_extract_system_prompt_instructs_benefits():
    assert "BENEFITS" in llm.EXTRACT_SYSTEM
    assert "Что мы предлагаем" in llm.EXTRACT_SYSTEM


def test_build_extract_prompt_schema_includes_benefits():
    prompt = llm.build_extract_prompt("post", "@c", None)
    assert "benefits" in prompt


def test_parse_extract_response_passes_benefits_through():
    raw = json.dumps({
        "title": "AI Eng", "benefits": ["ДМС / медстраховка", "Обучение"],
    })
    res = llm.parse_extract_response(raw, "@c", None)
    assert res.benefits == ["ДМС / медстраховка", "Обучение"]


def test_parse_extract_response_defaults_benefits_empty():
    raw = json.dumps({"title": "AI Eng"})
    res = llm.parse_extract_response(raw, "@c", None)
    assert res.benefits == []


# --- SCORING UNAFFECTED (critical) -----------------------------------------


def test_build_score_prompt_excludes_benefits():
    ex = ExtractResult(
        title="AI Eng", source_channel="@c", stack=["python", "llm"],
        benefits=["ДМС / медстраховка", "Обучение", "Визовый спонсор"],
    )
    prompt = llm.build_score_prompt(ex, raw_text="some posting body, no benefit words here")
    # The benefit LABELS must not appear in the structured score payload.
    # (raw_text is passed verbatim; we deliberately keep benefit words out of it.)
    assert "ДМС" not in prompt
    assert "Обучение" not in prompt
    assert "Визовый спонсор" not in prompt
    assert "benefits" not in prompt


def test_build_score_prompt_identical_with_and_without_benefits():
    """STRONGEST proof: score payload is byte-identical regardless of benefits."""
    common = dict(
        title="AI Eng", source_channel="@c", company="Acme",
        stack=["python", "llm"], seniority="middle", remote=True,
        relocation=True, location="Москва",
    )
    raw = "identical posting body"
    with_benefits = ExtractResult(**common, benefits=["ДМС / медстраховка", "Обучение"])
    without_benefits = ExtractResult(**common, benefits=[])
    assert (
        llm.build_score_prompt(with_benefits, raw)
        == llm.build_score_prompt(without_benefits, raw)
    )


def test_scoring_module_has_no_benefits_reference():
    import inspect

    from job_hunter import scoring

    assert "benefits" not in inspect.getsource(scoring)
