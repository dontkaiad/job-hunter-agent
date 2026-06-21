"""LLM prompt-building + response-parsing (pure) and wrappers with FakeLLM."""

import json

from job_hunter import llm
from job_hunter.schema_extract import ExtractResult


def test_parse_json_object_with_fence():
    text = "Here:\n```json\n{\"relevance_score\": 80, \"Обоснование\": \"good\"}\n```"
    out = llm.parse_score_response(text)
    assert out["score"] == 80
    assert out["reasoning"] == "good"


def test_parse_extract_response_fills_defaults():
    text = '{"title": "AI Eng", "stack": ["python", "llm"], "salary_max": 300000, "currency": "RUB"}'
    r = llm.parse_extract_response(text, "@c", "http://x")
    assert isinstance(r, ExtractResult)
    assert r.source_channel == "@c"
    assert r.source_link == "http://x"
    assert r.relevance_score is None
    assert r.stack == ["python", "llm"]


def test_llm_extract_wrapper_calls_client(fake_llm):
    fake_llm.set_for("strict JSON", '{"title":"X","stack":["python"]}')
    r = llm.llm_extract(fake_llm, "raw post", "@c", None)
    assert r.title == "X"
    assert fake_llm.calls  # was called
    assert "JOB POST" in fake_llm.calls[0]["user"]


def test_llm_extract_routes_to_cheap_model(fake_llm):
    fake_llm.set_for("strict JSON", '{"title":"X","stack":["python"]}')
    llm.llm_extract(fake_llm, "raw post", "@c", None)
    assert fake_llm.calls[0]["model"] == llm.CHEAP_MODEL


def test_llm_score_wrapper_returns_score_and_reasoning(fake_llm):
    fake_llm.set_for(
        "hiring-fit JUDGE",
        '{"relevance_score": 30, "Обоснование": "это про обучение моделей"}',
    )
    ex = ExtractResult(title="t", source_channel="@c")
    out = llm.llm_score(fake_llm, ex, "raw post")
    assert out["score"] == 30
    assert "обучение" in out["reasoning"]


def test_llm_score_routes_to_judge_model(fake_llm):
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 70, "Обоснование": "ok"}')
    ex = ExtractResult(title="t", source_channel="@c")
    llm.llm_score(fake_llm, ex, "raw")
    assert fake_llm.calls[0]["model"] == llm.JUDGE_MODEL


def test_parse_score_clamps_out_of_range():
    out = llm.parse_score_response('{"relevance_score": 250, "Обоснование": "x"}')
    assert out["score"] == 100
    out2 = llm.parse_score_response('{"relevance_score": -10, "Обоснование": "x"}')
    assert out2["score"] == 0


def test_parse_score_missing_score_raises():
    import pytest
    with pytest.raises(ValueError):
        llm.parse_score_response('{"Обоснование": "no score here"}')


def test_parse_score_keeps_multiline_bullet_reasoning_intact():
    """parse_score_response returns the «Обоснование» string AS-IS — now a
    verdict line + ✅/⚠️ full-sentence bullets (multi-line) — contract unchanged
    (still returns the raw str, only .strip()'d)."""
    block = (
        "Сильный фит по роли — 82/100\n"
        "✅ Роль — про внедрение LLM (RAG, роутинг) в ядре, что совпадает с её "
        "хендз-он опытом.\n"
        "⚠️ Требуется свободный английский прямо сейчас, что слегка снижает оценку."
    )
    raw = '{"relevance_score": 82, "Обоснование": ' + json.dumps(block, ensure_ascii=False) + "}"
    out = llm.parse_score_response(raw)
    assert out["score"] == 82
    assert out["reasoning"] == block
    # The full multi-line bullet string survives intact (no reformatting).
    assert "82/100" in out["reasoning"]
    assert "✅" in out["reasoning"] and "⚠️" in out["reasoning"]
    assert "\n" in out["reasoning"]  # stays multi-line


def test_score_system_constrains_verdict_plus_bullets_reasoning():
    """Prompt-content guard so the OUTPUT-FORMAT instruction can't silently
    regress. «Обоснование» must be a VERDICT line (with the score number) plus
    2-4 ✅/⚠️ bullets, each a FULL SENTENCE (NOT a fragment), in the post's
    language. It must NO LONGER mandate the flowing-paragraph format, and must
    explicitly forbid both terse fragments and a wall/paragraph. Does NOT touch
    the rubric/profile wording."""
    s = llm.SCORE_SYSTEM
    low = s.lower()
    # New format: a verdict line that includes the score number.
    assert "verdict" in low
    assert "score number" in low
    # 2-4 ✅/⚠️ bullets.
    assert "2-4" in low
    assert "bullet" in low
    assert "✅" in s and "⚠️" in s
    # Each bullet a FULL SENTENCE (substantive, self-contained).
    assert "full" in low and "sentence" in low
    assert "self-contained" in low
    # Fragments are explicitly FORBIDDEN (so "too terse fragments" can't return),
    # spelled out with a good vs bad example.
    assert "fragment" in low
    assert "forbidden" in low
    assert "good" in low and "bad" in low  # the good-vs-bad example is present
    # A flowing-paragraph / wall is explicitly forbidden too.
    assert "paragraph" in low or "wall" in low
    # The OLD flowing-paragraph MANDATE is GONE.
    assert "flowing sentences" not in low
    assert "2-3 full, flowing" not in low
    assert "not ✅/⚠️ bullet lists" not in low  # the old "no bullets" line is gone
    # Must cover core fit (role/stack/level) AND the main caveat/risk.
    assert "core fit" in low
    assert "caveat" in low or "risk" in low
    # Same language as the posting.
    assert "same language as the posting" in low
    # JSON contract still present and unchanged.
    assert '"relevance_score"' in s
    assert "Обоснование" in s
    # Rubric/profile invariants still intact (presentation-only change). The
    # module constant is rendered from the GENERIC example profile, so the floor
    # is the generic EUR 1000, never a personal figure.
    assert "CANDIDATE PROFILE" in s
    assert "RUBRIC" in s
    assert "EUR 1000" in s  # example profile floor; no personal figure
    assert "EUR " + "25" + "00" not in s
    assert "STRONG FIT" in s
    assert "LOCATION PRIORITY" in s
    assert "do not consider salary" in low


def test_extract_system_scans_whole_post_for_contact():
    """FIX 3: the extract prompt must tell the model to scan the WHOLE post
    (including the bottom) for the contact and never use the source channel."""
    s = llm.EXTRACT_SYSTEM
    low = s.lower()
    assert "whole post" in low or "scan" in low
    assert "bottom" in low or "last lines" in low
    assert "not the source channel" in low or "never use the source channel" in low
    # Email mapping documented: bare email -> contact_type null.
    assert "bare email" in low and "null" in low


def test_extract_system_reads_hashtags_benefits_and_full_body_for_remote():
    """EXTRACT fix: the prompt must instruct reading hashtags + the benefits
    «Что мы предлагаем» block + the full body for remote/location/seniority."""
    s = llm.EXTRACT_SYSTEM
    low = s.lower()
    assert "hashtag" in low
    assert "что мы предлагаем" in low or "benefits" in low
    assert "full post" in low or "read the full post" in low
    # remote/location/seniority all addressed.
    assert "remote" in low and "location" in low and "seniority" in low
    # hybrid handling documented (remote true + (гибрид) on location).
    assert "hybrid" in low or "гибрид" in low
    # concrete RU hashtag examples present.
    assert "удаленкарф" in low or "#москва" in low


def test_parse_extract_strips_channel_leaked_into_contact():
    # The model echoed the channel as the contact -> must be nulled out.
    text = '{"title":"X","stack":["python"],"contact":"@jobschan","contact_type":"dm"}'
    r = llm.parse_extract_response(text, "@jobschan", "https://t.me/jobschan/5")
    assert r.contact is None
    assert r.contact_type is None


def test_parse_extract_keeps_real_body_contact():
    text = '{"title":"X","stack":["python"],"contact":"@real_hr","contact_type":"dm"}'
    r = llm.parse_extract_response(text, "@jobschan", "https://t.me/jobschan/5")
    assert r.contact == "@real_hr"
    assert r.contact_type == "dm"


def test_llm_research_wrapper(fake_llm):
    fake_llm.set_for("research", '{"summary":"s","talking_points":["a"],"questions":["q"]}')
    ex = ExtractResult(title="t", source_channel="@c")
    out = llm.llm_research(fake_llm, ex, "raw")
    assert out["summary"] == "s"
    assert out["talking_points"] == ["a"]


def test_llm_draft_wrapper(fake_llm):
    fake_llm.set_for("application message", "Hi, I am interested.")
    ex = ExtractResult(title="t", source_channel="@c")
    out = llm.llm_draft(fake_llm, ex, "raw")
    assert out == "Hi, I am interested."


# --- FIX 1: DRAFT prompt mandates feminine Russian self-reference -----------

def test_draft_system_gender_rule_is_profile_driven():
    """The gender rule is injected from the loaded profile, NOT hardcoded.

    The module default DRAFT_SYSTEM is rendered from the GENERIC example profile
    (gender 'unspecified'), so it must NOT assert the candidate is a WOMAN.
    Building from a 'female' profile renders the feminine-forms rule; building
    from 'male' renders masculine. English is always exempted.
    """
    from job_hunter.profile import Profile

    # Default (example, unspecified): no personal gender claim.
    s_default = llm.DRAFT_SYSTEM
    assert "WOMAN" not in s_default
    assert "unspecified" in s_default.lower()

    # Female profile -> feminine forms spelled out so it can't regress.
    s_f = llm.build_draft_system(Profile(gender="female"))
    low_f = s_f.lower()
    assert "woman" in low_f
    assert "feminine" in low_f
    assert "заинтересована" in s_f
    assert "работала" in s_f
    assert "готова" in s_f
    assert "english" in low_f  # English explicitly exempted

    # Male profile -> masculine forms.
    s_m = llm.build_draft_system(Profile(gender="male"))
    assert "MAN" in s_m
    assert "заинтересован" in s_m


# --- FIX 2: DRAFT prompt forbids overclaiming (evals / Python-from-scratch) -

def test_draft_system_forbids_overclaiming_evals_and_python():
    s = llm.DRAFT_SYSTEM
    low = s.lower()
    # Accurate representation, no inflation.
    assert "accurately" in low or "accurate" in low
    assert "never inflate" in low or "not inflate" in low or "never invent" in low
    # Evals are in-development, not hands-on.
    assert "eval" in low
    assert "in development" in low or "in-development" in low or "learning" in low
    # Python: reads/reviews/architects, not from scratch.
    assert "from scratch" in low
    assert "reads" in low or "review" in low or "architect" in low


# --- FIX 4: DRAFT prompt reads full JD; only asks genuinely-missing things --

def test_draft_system_only_asks_missing_questions():
    s = llm.DRAFT_SYSTEM
    low = s.lower()
    assert "full vacancy text" in low or "read the full" in low
    assert "genuinely missing" in low or "genuinely" in low
    # Drop the questions if everything is covered.
    assert "drop the questions" in low
    # Example: don't re-ask frameworks/vector db the post already lists.
    assert "langchain" in low or "vector db" in low


# --- AUDIENCE: written for a non-technical recruiter, questions capped -------

def test_draft_system_targets_nontechnical_recruiter():
    s = llm.DRAFT_SYSTEM
    low = s.lower()
    # Audience is a non-technical recruiter; lead with plain-language fit.
    assert "non-technical recruiter" in low
    assert "plain-language fit" in low or "plain language" in low
    # Cap clarifying questions at one-simple-max, prefer none.
    assert "at most one" in low
    assert "prefer asking none" in low or "default to none" in low
    # Forbid recruiter-incomprehensible technical questions with the example.
    assert "forbidden" in low
    assert "human eval" in low  # appears as a NEGATIVE / forbidden example
    assert "автоматические метрики или active human eval" in s


# --- TERMS: Cyrillic-transliterated common terms, not a Latin salad ---------

def test_draft_system_prefers_cyrillic_transliterated_terms():
    s = llm.DRAFT_SYSTEM
    low = s.lower()
    # Preferred Cyrillic-transliterated common terms.
    assert "промпты" in s
    assert "воркфлоу" in s
    assert "эвалы" in s
    # Warns against the Latin-script salad.
    assert "latin-script salad" in low or "latin salad" in low or "latin words" in low
    # Short allowlist that stays Latin.
    assert "RAG" in s and "LLM" in s and "Docker" in s and "API" in s
    # GOOD vs BAD example present.
    assert "строю RAG-пайплайны" in s
    assert "active human eval in" in s  # the BAD example


# --- DE-SLOP: must not read as AI-generated ---------------------------------

def test_draft_system_de_slops_against_ai_tells():
    s = llm.DRAFT_SYSTEM
    low = s.lower()
    # Short / specific / natural / concise instruction.
    assert "short" in low
    assert "specific" in low
    assert "natural" in low
    assert "concise" in low
    # Forbid the over-polished tri-paragraph shape.
    assert "tri-paragraph" in low
    # Forbid specific AI-slop tells.
    assert "звучит интересно" in s
    assert "был(а) бы рад(а) возможности" in s
    assert "hedging" in low


def test_build_draft_prompt_includes_full_raw_text():
    ex = ExtractResult(title="AI Eng", source_channel="@c", stack=["python", "rag"])
    raw = (
        "AI Engineer.\nStack: Python, LangChain, Qdrant vector DB.\n"
        "Salary 300000 RUB. Remote.\nUNIQUE_MARKER_RAWTEXT_12345 at the bottom."
    )
    prompt = llm.build_draft_prompt(ex, raw)
    # The COMPLETE raw_text must be threaded into the prompt (not just fields),
    # so the model can tell what the JD already answers.
    assert raw in prompt
    assert "UNIQUE_MARKER_RAWTEXT_12345" in prompt
    assert "raw_text" in prompt.lower() or "full vacancy" in prompt.lower()


def test_llm_draft_threads_raw_text_into_call(fake_llm):
    fake_llm.set_for("application message", "Здравствуйте, мне интересно.")
    ex = ExtractResult(title="t", source_channel="@c")
    raw = "Полное описание вакансии MARKER_BODY_99 с деталями стека."
    llm.llm_draft(fake_llm, ex, raw)
    assert "MARKER_BODY_99" in fake_llm.calls[0]["user"]


# --- DRAFT body: company naming + links/placeholder (no fabricated URL) ------

def test_draft_system_instructs_company_links_and_resume_placeholder():
    s = llm.DRAFT_SYSTEM
    low = s.lower()
    # Company addressing + graceful null handling (no literal "None").
    assert "target company" in low
    assert "none" in low  # forbids writing the literal placeholder word
    # GitHub link is injected from the (example) profile -> generic placeholder,
    # NOT a personal handle. It forbids fabricating a resume URL.
    assert "github.com/example" in s
    assert "github.com/" + "dont" + "kaiad" not in s
    assert "do not fabricate" in low or "do not invent" in low or "not invent" in low
    assert "резюме" in s


def test_build_draft_prompt_names_company_when_present():
    ex = ExtractResult(title="AI Eng", company="Нетбелл", source_channel="@c")
    prompt = llm.build_draft_prompt(ex, "raw")
    assert "Нетбелл" in prompt
    assert "TARGET COMPANY" in prompt


def test_build_draft_prompt_company_null_no_none_literal():
    ex = ExtractResult(title="AI Eng", company=None, source_channel="@c")
    prompt = llm.build_draft_prompt(ex, "raw")
    # company is null -> the prompt instructs a neutral opening and must NOT
    # surface a bare "None" company line for the model to copy.
    assert "neutral opening" in prompt
    # The JSON payload still carries company:null, but no human-facing
    # "TARGET COMPANY (address ...): None" instruction line.
    assert "address the отклик to it): None" not in prompt
