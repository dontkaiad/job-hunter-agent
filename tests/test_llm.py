"""LLM prompt-building + response-parsing (pure) and wrappers with FakeLLM."""

import json

from job_hunter import llm
from job_hunter.schema_extract import ExtractResult


def test_parse_json_object_with_fence():
    text = "Here:\n```json\n{\"relevance_score\": 80, \"Обоснование\": \"good\"}\n```"
    out = llm.parse_score_response(text)
    assert out["score"] == 80
    assert out["reasoning"] == "good"


# --- SHARED PARSER hardening: _parse_json_object (benefits all 3 callers) ----


def test_parse_json_object_complete_json_fence_parses():
    """(a) A standard ```json ... ``` fenced COMPLETE object parses."""
    text = '```json\n{"a": 1, "b": "x"}\n```'
    assert llm._parse_json_object(text) == {"a": 1, "b": "x"}


def test_parse_json_object_bare_triple_backtick_fence_parses():
    """(a') A ``` (no 'json' tag) fenced COMPLETE object parses."""
    text = '```\n{"a": 1}\n```'
    assert llm._parse_json_object(text) == {"a": 1}


def test_parse_json_object_leading_fence_only_no_close_parses():
    """(b) NEW hardening: a leading-fence-only response (closing ``` truncated
    away) with a COMPLETE object still parses. Before the preprocess fix the
    complete-fence regex needed BOTH fences, so this branch fell through to the
    object regex — which matched here only because the object itself is complete.
    The preprocess strip makes the leading marker handling explicit/robust."""
    text = '```json\n{"summary": "s", "talking_points": ["a", "b"]}'
    out = llm._parse_json_object(text)
    assert out == {"summary": "s", "talking_points": ["a", "b"]}


def test_parse_json_object_fence_markers_on_own_lines_parses():
    """(b') Odd placement: fence markers on their own lines around the object."""
    text = "```\n\n{\n  \"x\": 1\n}\n\n```"
    assert llm._parse_json_object(text) == {"x": 1}


def test_parse_json_object_unfenced_parses_identically_no_behavior_change():
    """(c) An ALREADY-UNFENCED object parses identically to before — the
    preprocess strip is a no-op when no fence marker is present (same dict)."""
    text = '{"relevance_score": 55, "Обоснование": "ровно"}'
    assert llm._parse_json_object(text) == {
        "relevance_score": 55,
        "Обоснование": "ровно",
    }


def test_parse_json_object_surrounding_prose_still_parses():
    """(d) JSON embedded in surrounding prose (no fence) still parses — the
    object regex finds the braces; the fence strip leaves prose untouched."""
    text = 'Sure, here is the result: {"a": 1, "b": [1, 2]} -- hope that helps!'
    assert llm._parse_json_object(text) == {"a": 1, "b": [1, 2]}


def test_parse_json_object_stray_backticks_in_prose_not_mistreated():
    """A stray trailing ``` inside unfenced prose must NOT be stripped as a
    close fence (we only strip a close when we stripped a leading open)."""
    text = 'Result {"a": 1} and see the ``` block below'
    assert llm._parse_json_object(text) == {"a": 1}


def test_parse_json_object_truncated_no_close_brace_raises():
    """(e) A genuinely TRUNCATED object (no closing brace) still raises
    ValueError cleanly — we cannot invent the missing data; the real fix is the
    upstream max_tokens bump. Mirrors the confirmed #20 cut-off shape."""
    import pytest

    truncated = '```json\n{"summary": "s", "talking_points": ['
    with pytest.raises(ValueError):
        llm._parse_json_object(truncated)


def test_parse_json_object_truncated_unfenced_no_close_brace_raises():
    import pytest

    with pytest.raises(ValueError):
        llm._parse_json_object('{"summary": "s", "questions":')


def test_all_three_parsers_share_central_json_parser(monkeypatch):
    """Confirm extract/score/research ALL route through _parse_json_object, so a
    central hardening benefits every caller. We spy on the shared parser."""
    seen = {"n": 0}
    real = llm._parse_json_object

    def _spy(text):
        seen["n"] += 1
        return real(text)

    monkeypatch.setattr(llm, "_parse_json_object", _spy)

    llm.parse_extract_response('{"title": "t", "stack": []}', "@c", None)
    llm.parse_score_response('{"relevance_score": 50, "Обоснование": "x"}')
    llm.parse_research_response('{"summary": "s"}')
    assert seen["n"] == 3  # all three went through the shared parser


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


# --- max_tokens headroom: per-step bumps (the #20 truncation fix) ------------


def test_llm_research_requests_raised_max_tokens(fake_llm):
    """research max_tokens raised 900 -> 2000 so a page-grounded COMPLETE JSON
    is not truncated mid-output (the confirmed #20 break cut at \"questions\":)."""
    fake_llm.set_for("research", '{"summary":"s","talking_points":["a"]}')
    ex = ExtractResult(title="t", source_channel="@c")
    llm.llm_research(fake_llm, ex, "raw")
    assert llm.RESEARCH_MAX_TOKENS == 2000
    assert fake_llm.calls[0]["max_tokens"] == 2000


def test_llm_score_requests_raised_max_tokens(fake_llm):
    """score max_tokens raised 400 -> 800: a verbose Cyrillic «Обоснование»
    (verdict + 2-4 full-sentence bullets, token-heavy) no longer truncates into a
    silent fallback score."""
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 70, "Обоснование": "ok"}')
    ex = ExtractResult(title="t", source_channel="@c")
    llm.llm_score(fake_llm, ex, "raw")
    assert llm.SCORE_MAX_TOKENS == 800
    assert fake_llm.calls[0]["max_tokens"] == 800


def test_llm_extract_requests_raised_max_tokens(fake_llm):
    """extract max_tokens raised 800 -> 1200: a rich post (long stack / benefits /
    reasons + company / contact) no longer truncates into the heuristic fallback
    (which also loses the contact-as-link URL #20 relies on)."""
    fake_llm.set_for("strict JSON", '{"title":"X","stack":["python"]}')
    llm.llm_extract(fake_llm, "raw post", "@c", None)
    assert llm.EXTRACT_MAX_TOKENS == 1200
    assert fake_llm.calls[0]["max_tokens"] == 1200


def test_llm_draft_max_tokens_unchanged(fake_llm):
    """draft returns raw text (no JSON parse) and ~90-150 words; 500 is ample and
    intentionally unchanged."""
    fake_llm.set_for("application message", "Hi.")
    ex = ExtractResult(title="t", source_channel="@c")
    llm.llm_draft(fake_llm, ex, "raw")
    assert llm.DRAFT_MAX_TOKENS == 500
    assert fake_llm.calls[0]["max_tokens"] == 500


# --- research end-to-end on a realistic COMPLETE (untruncated) response -------


def test_parse_research_response_populated_and_talking_points_capped():
    """A realistic COMPLETE page-grounded JSON yields populated summary /
    talking_points / sourced_facts; talking_points is capped at <=6."""
    raw = json.dumps(
        {
            "summary": "Acme builds RAG tooling for banks.",
            "talking_points": ["t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8"],
            "questions": ["q1"],
            "sourced_facts": ["Acme raised a Series A (from the about page)."],
        },
        ensure_ascii=False,
    )
    out = llm.parse_research_response(raw)
    assert out["summary"] == "Acme builds RAG tooling for banks."
    assert len(out["talking_points"]) == 6  # capped <= 6
    assert out["talking_points"] == ["t1", "t2", "t3", "t4", "t5", "t6"]
    assert out["sourced_facts"] == ["Acme raised a Series A (from the about page)."]


def test_research_system_instructs_raw_json_and_caps_talking_points():
    """Defense in depth: the research prompt must instruct RAW JSON (no code
    fence) AND cap talking_points to <= 6, keeping the existing JSON contract."""
    s = llm.RESEARCH_SYSTEM
    low = s.lower()
    assert "raw json" in low
    assert "no code fence" in low or "no markdown" in low
    assert "do not wrap it in" in low  # explicit "no ```" instruction
    # talking_points capped at most 6.
    assert "at most 6" in low or "<= 6" in low or "6 items" in low
    # JSON contract unchanged.
    assert '"summary"' in s
    assert '"talking_points"' in s
    assert '"questions"' in s
    assert '"sourced_facts"' in s


# --- score: fenced regression + verbose rationale within new limit -----------


def test_parse_score_fenced_response_parses_regression():
    text = '```json\n{"relevance_score": 64, "Обоснование": "норм фит"}\n```'
    out = llm.parse_score_response(text)
    assert out["score"] == 64
    assert out["reasoning"] == "норм фит"


def test_parse_score_verbose_cyrillic_rationale_parses_and_clamps():
    """A verbose «Обоснование» (verdict + several full-sentence Cyrillic bullets)
    within the new limit parses and the score clamps correctly."""
    block = (
        "Сильный фит по роли — 120/100\n"
        "✅ Роль про внедрение LLM (RAG, роутинг, промпт-инжиниринг) в ядре, "
        "что напрямую совпадает с её хендз-он опытом по построению пайплайнов.\n"
        "✅ Стек (Python-ревью, Qdrant, FastAPI) пересекается с тем, что компания "
        "использует, поэтому адаптация будет быстрой.\n"
        "⚠️ Требуется свободный английский прямо сейчас, что является текущим "
        "пробелом и немного снижает итоговую оценку.\n"
        "⚠️ Грейд ближе к синьорному с требованием формального стажа, что добавляет риск."
    )
    raw = '{"relevance_score": 120, "Обоснование": ' + json.dumps(block, ensure_ascii=False) + "}"
    out = llm.parse_score_response(raw)
    assert out["score"] == 100  # clamped from 120
    assert out["reasoning"] == block
    assert "✅" in out["reasoning"] and "⚠️" in out["reasoning"]


# --- extract: fenced regression + unchanged normal parsing -------------------


def test_parse_extract_fenced_response_parses():
    text = '```json\n{"title": "AI Eng", "stack": ["python", "llm"]}\n```'
    r = llm.parse_extract_response(text, "@c", "http://x")
    assert isinstance(r, ExtractResult)
    assert r.title == "AI Eng"
    assert r.stack == ["python", "llm"]


def test_parse_extract_normal_response_unchanged():
    """Normal (unfenced) extract parsing is unchanged by the parser hardening."""
    text = '{"title": "X", "stack": ["python"], "contact": "@real_hr", "contact_type": "dm"}'
    r = llm.parse_extract_response(text, "@chan", "https://t.me/chan/9")
    assert r.title == "X"
    assert r.contact == "@real_hr"
    assert r.contact_type == "dm"


# ---------------------------------------------------------------------------
# Adversarial edge-case tests added by Tester (adversarial validation pass)
# ---------------------------------------------------------------------------


def test_parse_json_object_crlf_fence_parses():
    """Windows-style CRLF after the opening fence tag must be stripped correctly."""
    text = "```json\r\n{\"a\": 1}\r\n```"
    assert llm._parse_json_object(text) == {"a": 1}


def test_parse_json_object_fence_no_newline_between_marker_and_object():
    """Fence marker with no newline before the object: ```json{"a": 1} must parse."""
    text = '```json{"a": 1}'
    assert llm._parse_json_object(text) == {"a": 1}


def test_parse_json_object_value_contains_triple_backtick_fenced():
    """A fenced response whose string VALUE contains triple-backtick must parse
    correctly and preserve the value intact — the trailing fence strip must only
    remove the closing code-fence marker, not the backticks inside a string."""
    reasoning = "See ```python code``` for the pattern."
    inner = json.dumps(
        {"relevance_score": 80, "Обоснование": reasoning}, ensure_ascii=False
    )
    fenced = "```json\n" + inner + "\n```"
    result = llm._parse_json_object(fenced)
    assert result["relevance_score"] == 80
    assert result["Обоснование"] == reasoning, (
        f"Triple-backtick inside value was corrupted: {result['Обоснование']!r}"
    )


def test_parse_json_object_greedy_over_capture_raises_not_wrong_dict():
    """The greedy _JSON_OBJ_RE (\{.*\} DOTALL) over-captures when there is a
    stray closing brace after the valid object in trailing prose.

    This is a confirmed limitation: the greedy match extends to the last } in
    the string, causing json.loads to fail with 'Extra data'. The key contract
    is that this raises ValueError (correct failure) — NOT a silently wrong
    partial dict. Any caller catching ValueError will degrade gracefully.
    """
    import pytest

    # Valid JSON object followed by a stray } in prose (e.g. a code snippet)
    text = '{"relevance_score": 70, "Обоснование": "good"} example code: f(x) }'
    with pytest.raises(ValueError):
        llm._parse_json_object(text)


def test_parse_json_object_greedy_two_objects_raises_not_first():
    """When a response contains two consecutive JSON objects (model misbehaves),
    the greedy regex captures from the first { to the last }, which spans both
    objects and fails json.loads — raising ValueError rather than silently
    returning the first (or second) object.

    Severity: LOW — this only happens when a model disobeys the 'Output ONLY
    JSON' instruction, and the error mode is a clean ValueError, not a wrong
    result."""
    import pytest

    text = '{"a": 1}\n{"b": 2}'
    with pytest.raises(ValueError):
        llm._parse_json_object(text)


def test_parse_json_object_trailing_fence_only_no_leading_left_alone():
    """A response that starts with a valid JSON object and ends with a trailing
    ``` (no leading fence) must NOT have the trailing ``` stripped — the
    trailing strip only fires when a leading fence was also found.

    In this case: {"a": 1}\n``` — the ``` contains no }, so the greedy regex
    correctly captures {"a": 1} which is the ONLY text from { to the last }.
    The result is {"a": 1} (the ``` is irrelevant to the object extraction).
    The trailing-strip guard is confirmed: no leading fence was stripped, so the
    trailing ``` is ignored by the preprocess, and the object extracts cleanly.
    """
    text = '{"a": 1}\n```'
    # The greedy \\{.*\\} captures {"a": 1} (the ``` has no }, so the last }
    # is the one that closes the JSON object). Parses correctly.
    result = llm._parse_json_object(text)
    assert result == {"a": 1}, (
        "Trailing-only ``` in unfenced prose must not prevent parsing the object"
    )


def test_parse_research_response_fenced_parses_regression():
    """Even though RESEARCH_SYSTEM instructs 'no fence', a fenced research
    response (model disobeyed the prompt) must still parse correctly — defense
    in depth so a single disobedient response does not crash the pipeline."""
    raw = json.dumps(
        {
            "summary": "Company builds RAG tools.",
            "talking_points": ["t1"],
            "questions": ["q1"],
            "sourced_facts": ["sf1"],
        },
        ensure_ascii=False,
    )
    fenced = "```json\n" + raw + "\n```"
    out = llm.parse_research_response(fenced)
    assert out["summary"] == "Company builds RAG tools."
    assert out["talking_points"] == ["t1"]
    assert out["sourced_facts"] == ["sf1"]


def test_parse_json_object_score_payload_real_shape_exact_equality():
    """Unfenced real-shaped score payload parses byte-identically to before the
    hardening — verifies the zero-diff / no-behavior-change contract for the
    most common production input shape."""
    payload = {
        "relevance_score": 82,
        "Обоснование": (
            "Сильный фит по роли — 82/100\n"
            "✅ Роль про внедрение LLM (RAG, роутинг) в ядре.\n"
            "⚠️ Требуется свободный английский прямо сейчас."
        ),
    }
    text = json.dumps(payload, ensure_ascii=False)
    result = llm._parse_json_object(text)
    assert result == payload, "Unfenced score payload must parse to exact-equality dict"


def test_parse_json_object_extract_payload_real_shape_exact_equality():
    """Unfenced real-shaped extract payload with arrays parses byte-identically."""
    payload = {
        "title": "AI Engineer",
        "company": "Acme",
        "stack": ["python", "llm", "rag", "fastapi"],
        "seniority": "middle",
        "salary_min": 200000.0,
        "salary_max": 350000.0,
        "currency": "RUB",
        "remote": True,
        "relocation": None,
        "location": "Москва",
        "contact_type": "dm",
        "contact": "@hr_acme",
        "reasons": ["Strong LLM fit", "Remote role"],
        "benefits": ["ДМС", "Обучение"],
    }
    text = json.dumps(payload, ensure_ascii=False)
    result = llm._parse_json_object(text)
    assert result == payload, "Unfenced extract payload must parse to exact-equality dict"


def test_parse_json_object_research_payload_real_shape_exact_equality():
    """Unfenced real-shaped research payload parses byte-identically."""
    payload = {
        "summary": "Acme builds RAG pipelines for enterprise clients.",
        "talking_points": [
            "Company focuses on applied LLM.",
            "Series A funded.",
        ],
        "questions": ["What is the team size?"],
        "sourced_facts": ["Acme raised Series A per their about page."],
    }
    text = json.dumps(payload, ensure_ascii=False)
    result = llm._parse_json_object(text)
    assert result == payload, "Unfenced research payload must parse to exact-equality dict"
