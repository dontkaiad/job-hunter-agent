"""The draft: a fixed greeting, no long / medium dashes (prompt rules + a deterministic safety net), honesty untouched."""

from __future__ import annotations

import pytest

from job_hunter import agents, llm
from job_hunter.profile import DraftSignature, Profile, example_profile
from job_hunter.schema_extract import ExtractResult

LONG_DASHES = ("—", "–")


# --- strip_long_dashes ---------------------------------------------------------------------------------


@pytest.mark.parametrize("src, want", [
    ("Строю RAG — пайплайны и эвалы — это моя работа.", "Строю RAG, пайплайны и эвалы, это моя работа."),
    ("Python – и FastAPI.", "Python, и FastAPI."),
    ("слово—слово", "слово, слово"),
    ("Готов обсудить —", "Готов обсудить."),
    ("I build RAG pipelines — fast — and evals.", "I build RAG pipelines, fast, and evals."),
])
def test_dash_between_words_becomes_a_comma(src, want):
    out = agents.strip_long_dashes(src)
    assert out == want
    assert not any(d in out for d in LONG_DASHES)


def test_digit_ranges_get_a_plain_hyphen_without_spaces():
    assert agents.strip_long_dashes("в 2022—2024 и 10 – 15 людей") == "в 2022-2024 и 10-15 людей"


def test_a_leading_dash_is_a_list_marker_and_is_dropped():
    assert agents.strip_long_dashes("— Первое\n— Второе") == "Первое\nВторое"


def test_a_spaced_hyphen_is_never_produced():
    for src in ("А — Б", "А – Б", "А—Б", "10 — 20", "— А", "А —"):
        out = agents.strip_long_dashes(src)
        assert " - " not in out and not any(d in out for d in LONG_DASHES), (src, out)


def test_text_without_long_dashes_is_returned_unchanged():
    src = "Кто-то пишет Python-код, и это нормально. Hyphen-ated words stay."
    assert agents.strip_long_dashes(src) == src
    assert agents.strip_long_dashes("") == ""


def test_no_leftover_punctuation_artefacts():
    out = agents.strip_long_dashes("Опыт — , а потом — . И всё — !")
    assert ", ," not in out and ",." not in out and ", !" not in out and ",," not in out


# --- ensure_greeting -----------------------------------------------------------------------------------


@pytest.mark.parametrize("src, lang, want", [
    ("Меня заинтересовала вакансия.", "ru", "Добрый день!\n\nМеня заинтересовала вакансия."),
    ("Здравствуйте! Меня заинтересовала вакансия.", "ru", "Добрый день!\n\nМеня заинтересовала вакансия."),
    ("Добрый день, команда X!\nМеня заинтересовала вакансия.", "ru", "Добрый день!\n\nМеня заинтересовала вакансия."),
    ("Привет! Я Python-разработчик.", "ru", "Добрый день!\n\nЯ Python-разработчик."),
    ("I saw the role.", "en", "Hello,\n\nI saw the role."),
    ("Hi there,\nI saw the role.", "en", "Hello,\n\nI saw the role."),
    ("Dear Hiring Manager,\n\nI saw the role.", "en", "Hello,\n\nI saw the role."),
    ("Hello team, I build RAG.", "en", "Hello,\n\nI build RAG."),
])
def test_first_line_is_exactly_the_greeting(src, lang, want):
    assert agents.ensure_greeting(src, lang) == want


def test_greeting_is_idempotent_and_empty_text_gets_the_greeting_only():
    once = agents.ensure_greeting("Текст.", "ru")
    assert agents.ensure_greeting(once, "ru") == once
    assert agents.ensure_greeting("", "en") == "Hello,"
    assert agents.ensure_greeting("Добрый день!", "ru") == "Добрый день!"


def test_a_sentence_after_hello_is_kept_not_swallowed():
    assert "I build RAG pipelines" in agents.ensure_greeting("Hello team, I build RAG pipelines for search", "en")


# --- polish_draft / draft() wiring ----------------------------------------------------------------------

RU_POST = "Ищем LLM-инженера в команду. Python, RAG, удалённо. Пишите нам."
EN_POST = "We are hiring an applied LLM engineer. Python, RAG, remote. Apply here."


def test_polish_uses_the_language_of_the_vacancy():
    assert agents.polish_draft("Текст — тут.", RU_POST).startswith("Добрый день!\n\n")
    assert agents.polish_draft("Text — here.", EN_POST).startswith("Hello,\n\n")
    assert "—" not in agents.polish_draft("Текст — тут.", RU_POST)


class _FakeClient:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def complete(self, system, user, **kw):
        self.calls.append((system, user))
        return self.reply


def _profile_with_site():
    base = example_profile()
    return Profile(**{**base.__dict__, "draft_signature": DraftSignature(github="github.com/x", resume="Сайт: example.test")})


def test_draft_returns_greeting_no_dashes_and_the_profile_signature_at_the_end():
    ex = ExtractResult(title="LLM engineer", source_channel="ch", company=None)
    client = _FakeClient("Здравствуйте! Строю RAG — пайплайны и эвалы — на Python.")
    out = agents.draft(client, ex, RU_POST, None, profile=_profile_with_site())
    assert out.startswith("Добрый день!\nОткликаюсь на позицию «LLM engineer». ")
    assert not any(d in out for d in LONG_DASHES)
    assert out.endswith("GitHub: github.com/x\nСайт: example.test")


def test_signature_block_is_github_plus_the_profile_line():
    assert _profile_with_site().draft_signature.block() == "GitHub: github.com/x\nСайт: example.test"


# --- the prompt ------------------------------------------------------------------------------------------


def test_prompt_carries_the_style_and_greeting_rules():
    s = llm.build_draft_system(example_profile())
    assert "GREETING AND INTRO" in s and "«Добрый день!»" in s and "«Hello,»" in s and "written by code" in s
    assert "NO long or medium dashes" in s and "never write — or –" in s
    for stock in ("рад(а) предложить свои навыки", "уверен(а), что смогу", "динамично развивающаяся"):
        assert stock in s
    assert "SHORT sentences" in s


def test_prompt_no_longer_tells_the_model_to_mention_a_resume_attachment():
    s = llm.build_draft_system(example_profile())
    assert "резюме во вложении" not in s


def test_honesty_and_never_claim_rules_are_unchanged():
    base = example_profile()
    prof = Profile(**{**base.__dict__, "never_claim": ["fine-tuning", "PyTorch"]})
    s = llm.build_draft_system(prof)
    assert "HONESTY (CRITICAL" in s and "HANDS-ON (may claim as real experience)" in s
    assert "NEVER CLAIM, not even as 'learning' or 'familiar with'" in s and "fine-tuning" in s and "PyTorch" in s
    assert "Never invent experience the candidate does not have" in s


def test_user_prompt_tells_the_model_what_code_already_wrote_per_language():
    ex = ExtractResult(title="LLM engineer", source_channel="ch", company=None)
    ru = llm.build_draft_prompt(ex, RU_POST, intro=llm.draft_intro(ex, RU_POST))
    en = llm.build_draft_prompt(ex, EN_POST, intro=llm.draft_intro(ex, EN_POST))
    assert "ALREADY WRITTEN ABOVE YOUR TEXT" in ru and "Добрый день!\nОткликаюсь на позицию «LLM engineer»." in ru
    assert "ALREADY WRITTEN ABOVE YOUR TEXT" in en and "Hello,\nI'm applying for the LLM engineer position." in en


def test_a_cut_greeting_leaves_a_sentence_that_starts_with_a_capital():
    assert agents.ensure_greeting("Здравствуйте, мне интересна позиция.", "ru") == "Добрый день!\n\nМне интересна позиция."
    assert agents.ensure_greeting("Hi I want to apply", "en") == "Hello,\n\nI want to apply"


# --- signature written by the model itself, foreign scripts ---------------------------------------------


def test_signature_lines_written_by_the_model_are_dropped_so_the_block_is_not_doubled():
    prof = _profile_with_site()
    body = "Добрый день!\n\nСтрою RAG.\n\nGitHub: github.com/x\nСайт: example.test\n[резюме: ссылка]\ngithub.com/x"
    out = agents.strip_own_signature(body, prof)
    assert out == "Добрый день!\n\nСтрою RAG."
    ex = ExtractResult(title="t", source_channel="ch", company=None)
    full = agents.draft(_FakeClient("Строю RAG.\n\nGitHub: github.com/x"), ex, RU_POST, None, profile=prof)
    assert full.count("GitHub: github.com/x") == 1 and full.endswith("GitHub: github.com/x\nСайт: example.test")


def test_ordinary_lines_that_mention_github_or_a_site_are_kept():
    keep = "Код лежит на GitHub, а сайт расскажет больше."
    assert agents.strip_own_signature(keep) == keep


def test_foreign_script_is_detected_regenerated_once_and_stripped_as_a_last_resort():
    assert agents.has_foreign_script("проектирую워크флоу") and agents.has_foreign_script("质量")
    assert not agents.has_foreign_script("Python, RAG, эвалы, Hello!")
    assert agents.strip_foreign_script("проектирую워크флоу") == "проектируюфлоу"

    class Seq:
        def __init__(self, replies):
            self.replies, self.n = list(replies), 0

        def complete(self, system, user, **kw):
            self.n += 1
            return self.replies.pop(0)

    ex = ExtractResult(title="t", source_channel="ch", company=None)
    ok = Seq(["Пишу промпты 워크.", "Пишу промпты."])
    assert "워" not in agents.draft(ok, ex, RU_POST, None) and ok.n == 2          # regenerated once, clean
    still = Seq(["Пишу 워크.", "Пишу 워크 снова."])
    out = agents.draft(still, ex, RU_POST, None)
    assert "워" not in out and still.n == 2                                         # still dirty: stripped, no third call
    clean = Seq(["Пишу промпты."])
    agents.draft(clean, ex, RU_POST, None)
    assert clean.n == 1                                                              # clean text: a single call


def test_prompt_forbids_invented_numbers_foreign_scripts_and_a_self_written_signature():
    s = llm.build_draft_system(example_profile())
    assert "NO invented numbers" in s and "Cyrillic and Latin letters" in s
    assert "do NOT write a GitHub line" in s


def test_a_short_line_that_only_points_at_the_github_link_is_dropped_a_long_sentence_is_kept():
    prof = _profile_with_site()
    assert agents.strip_own_signature("Строю RAG.\n\nCode on GitHub at github.com/x.", prof) == "Строю RAG."
    long_line = "Строю пайплайны для поиска по документам, примеры лежат тут: github.com/x, там же проверки и тексты."
    assert agents.strip_own_signature(long_line, prof) == long_line


# --- the fixed intro (built by code, from the card's role and the profile) -------------------------------------

from job_hunter.profile import DraftIntro, parse_profile  # noqa: E402


def _ex(title):
    return ExtractResult(title=title, source_channel="ch", company=None)


def _profile_with_intro():
    base = example_profile()
    return Profile(**{**base.__dict__, "draft_intro": DraftIntro(
        ru="Я инженер, семь лет внедряю ИИ.", en="I'm an engineer with seven years of AI work.")})


def test_intro_russian_with_role_and_the_profile_sentence():
    intro = llm.draft_intro(_ex("LLM-инженер"), RU_POST, _profile_with_intro())
    assert intro == "Откликаюсь на позицию «LLM-инженер». Я инженер, семь лет внедряю ИИ."


def test_intro_english_with_role_and_the_profile_sentence():
    intro = llm.draft_intro(_ex("Applied AI Engineer"), EN_POST, _profile_with_intro())
    assert intro == "I'm applying for the Applied AI Engineer position. I'm an engineer with seven years of AI work."


@pytest.mark.parametrize("title", [None, "", "   ", "none", "https://example.test/jobs/1", "vacancy #project #city",
                                   "Company | Role | Remote", "AI Engineer / Python Developer | LLM Agents", "💬 ", "x" * 120])
def test_intro_with_an_empty_or_garbage_role_uses_the_generic_sentence(title):
    assert llm.draft_intro(_ex(title), RU_POST, _profile_with_intro()) == "Откликаюсь на вашу вакансию. Я инженер, семь лет внедряю ИИ."
    assert llm.draft_intro(_ex(title), EN_POST, _profile_with_intro()) == (
        "I'm applying for your open position. I'm an engineer with seven years of AI work.")


def test_role_is_cleaned_not_just_accepted():
    assert llm.clean_role("💬 ИИ-разработчик / Вайбкодер") == "ИИ-разработчик"       # the first alternative
    assert llm.clean_role('Senior "Ruby" Engineer.') == "Senior Ruby Engineer"
    assert llm.clean_role("«Prompt Engineer»") == "Prompt Engineer"
    assert llm.clean_role("AI Engineer (New Grads & Principal/Distinguished)") == "AI Engineer"


def test_the_years_come_from_the_profile_not_from_public_code():
    """No years / numbers in the public code path: change the profile sentence and the intro follows."""
    a = llm.draft_intro(_ex("Role X"), RU_POST, _profile_with_intro())
    other = Profile(**{**example_profile().__dict__, "draft_intro": DraftIntro(ru="Я разработчик.", en="I'm a developer.")})
    b = llm.draft_intro(_ex("Role X"), RU_POST, other)
    assert "семь лет" in a and "семь лет" not in b and b.endswith("Я разработчик.")
    assert "лет" not in llm.draft_intro(_ex("Role X"), RU_POST, example_profile())       # the generic placeholder has none


def test_profile_reads_draft_intro_and_has_generic_defaults():
    p = parse_profile({"draft_intro": {"ru": "Я X.", "en": "I'm X."}})
    assert (p.draft_intro.ru, p.draft_intro.en) == ("Я X.", "I'm X.")
    d = parse_profile({})
    assert d.draft_intro == DraftIntro()
    assert parse_profile({"draft_intro": "not a mapping"}).draft_intro == DraftIntro()


def test_composed_draft_is_greeting_then_intro_then_the_model_text():
    prof = _profile_with_intro()
    ex = _ex("LLM engineer")
    out = agents.draft(_FakeClient("Строю RAG — пайплайны и эвалы."), ex, RU_POST, None, profile=prof)
    lines = out.split("\n")
    assert lines[0] == "Добрый день!"
    assert lines[1] == "Откликаюсь на позицию «LLM engineer». Я инженер, семь лет внедряю ИИ."
    assert lines[2] == "" and lines[3] == "Строю RAG, пайплайны и эвалы."
    en = agents.draft(_FakeClient("I build RAG pipelines."), _ex("Applied AI Engineer"), EN_POST, None, profile=prof)
    assert en.split("\n")[:2] == ["Hello,", "I'm applying for the Applied AI Engineer position. I'm an engineer with seven years of AI work."]


def test_a_model_that_repeats_the_greeting_and_the_intro_is_cut_back_to_its_body():
    intro = "Откликаюсь на позицию «X». Я инженер."
    for body in ("Здравствуйте! Откликаюсь на позицию «X». Я инженер, семь лет в ИИ. Строю RAG.",
                 "Добрый день!\nОткликаюсь на вашу вакансию. Строю RAG.",
                 "Я инженер, работаю с ИИ. Строю RAG."):
        out = agents.compose_draft(body, "ru", intro)
        assert out.startswith(f"Добрый день!\n{intro}\n\nСтрою RAG"), out
    assert agents.compose_draft("", "ru", intro) == f"Добрый день!\n{intro}"
    en = agents.compose_draft("I'm applying for the role. I'm an AI engineer. I build RAG.", "en", "I'm applying for the X position. I'm an engineer.")
    assert en.endswith("\n\nI build RAG.")


def test_empty_role_in_a_full_draft_gives_the_generic_application_sentence():
    out = agents.draft(_FakeClient("Строю RAG."), _ex(None), RU_POST, None, profile=_profile_with_intro())
    assert out.split("\n")[1] == "Откликаюсь на вашу вакансию. Я инженер, семь лет внедряю ИИ."


# --- the new prompt rules -----------------------------------------------------------------------------------


def test_prompt_length_rule_and_no_greeting_or_intro_for_the_model():
    s = llm.build_draft_system(example_profile())
    assert "2 to 4 sentences, up to about 90 words" in s and "4 to 6 sentences, about 120 words" in s
    assert "do NOT greet" in s and "do NOT introduce yourself" in s and "do NOT repeat the intro" in s


def test_prompt_bans_empty_tails_company_compliments_and_opinions():
    s = llm.build_draft_system(example_profile())
    assert "NO empty tails" in s and "Готова развиваться" in s and "Заинтересована в позиции" in s
    assert "NO compliments to the company" in s and "not maintaining legacy code" in s and "the bar for craft" in s
    assert "ONE concrete detail taken from the vacancy" in s and "no closing line at all" in s
    assert "ONE short sentence of at most 15 words" in s and "NO statements of values or preferences" in s
    assert "Each sentence is up to about 25 words" in s


def test_in_development_items_may_only_be_learning_never_done():
    base = example_profile()
    prof = Profile(**{**base.__dict__, "in_development": ["local LLM inference"]})
    s = llm.build_draft_system(prof)
    assert "local LLM inference" in s
    assert "ONLY as «учусь» / «экспериментирую»" in s and "NEVER as «делала», «запускала»" in s
    assert "Never invent experience the candidate does not have" in s            # the older honesty rule is still there


# --- empty closing sentences and GitHub mentions in the body -------------------------------------------------------


@pytest.mark.parametrize("src, want", [
    ("Строю RAG. Гоняю эвалы. Код на GitHub, готова обсудить детали.", "Строю RAG. Гоняю эвалы."),
    ("Строю RAG. Гоняю эвалы. Готова к полностью удалённому формату.", "Строю RAG. Гоняю эвалы."),
    ("Строю RAG. Мне важна архитектура решений, которые работают в боевых условиях.", "Строю RAG."),
    ("I built RAG. The stack here lines up with what I've been doing, and I'd be interested in discussing it.", "I built RAG."),
    ("I built RAG. I'm excited about this role.", "I built RAG."),
    ("Строю RAG.\n\nЗаинтересована в позиции.", "Строю RAG."),
])
def test_empty_closing_sentences_are_cut(src, want):
    assert agents.strip_empty_tail(src) == want


def test_sentences_about_github_are_cut_anywhere_but_github_actions_stays():
    src = "Строю RAG. На GitHub есть примеры промптов. Деплой через GitHub Actions."
    assert agents.strip_empty_tail(src) == "Строю RAG. Деплой через GitHub Actions."


def test_a_substantive_last_sentence_and_a_lone_sentence_are_kept():
    keep = "Строю RAG-пайплайны. В Acme готова строить агентские воркфлоу."
    assert agents.strip_empty_tail(keep) == keep                  # "в Acme готова ..." is not an empty tail
    assert agents.strip_empty_tail("Готова обсудить.") == "Готова обсудить."          # never empties the text
    assert agents.strip_empty_tail("") == ""


def test_paragraph_structure_survives():
    src = "Первый абзац. Ещё фраза.\n\nВторой абзац.\n\nГотова обсудить."
    assert agents.strip_empty_tail(src) == "Первый абзац. Ещё фраза.\n\nВторой абзац."


def test_composed_draft_has_no_empty_tail_and_no_github_sentence():
    out = agents.draft(_FakeClient("Строю RAG. Код на GitHub. Готова обсудить детали."), _ex("Role X"), RU_POST, None,
                       profile=_profile_with_intro())
    assert "Готова обсудить" not in out and out.split("\n")[3] == "Строю RAG."
    assert out.count("GitHub") == 1                                  # only the signature line


def test_prompt_no_longer_lets_the_model_mention_github_or_the_site():
    s = llm.build_draft_system(example_profile())
    assert "Do NOT mention GitHub, the site, a portfolio" in s and "refer to it in words" not in s



@pytest.mark.parametrize("title, want", [
    ("AI Engineer (New Grads & Principal/Distinguished)", "AI Engineer"),          # brackets go with their content
    ("Full Stack Engineer [Hybrid] (AI)", "Full Stack Engineer"),
    ("(Remote) Python Developer", "Python Developer"),
    ("Разработчик python (ии-агенты)/Middle+", "Разработчик python"),               # a slash tail after a bracket
    ("Prompt Engineer / инженер AI-решений", "Prompt Engineer"),                    # alternatives: the first one
    ("ML Python Developer - Remote Europe", "ML Python Developer"),                 # a dash tail
    ("Senior AI Engineer \u2013 2 000 \u2013 3 000 USD", "Senior AI Engineer"),
    ("Software Engineer, AI Platform", "Software Engineer"),                         # a comma tail
    ("AI Engineer: LLM agents & RAG", "AI Engineer"),                                # a colon tail
    ("AI/ML Engineer", "AI/ML Engineer"),                                            # a slash inside a word stays
    ("Python-разработчик", "Python-разработчик"),                                    # a hyphen inside a word stays
    ("LLM-инженер", "LLM-инженер"),
])
def test_role_brackets_and_tails_are_cut(title, want):
    assert llm.clean_role(title) == want


@pytest.mark.parametrize("title", ["[Hybrid]", "(Remote)", " - Remote", "Role | Company", "a/b", "AI Engineer " + "word " * 12])
def test_role_that_is_nothing_after_the_cut_falls_back(title):
    cleaned = llm.clean_role(title)
    assert cleaned == "" or len(cleaned) >= 3
    if title in ("[Hybrid]", "(Remote)", " - Remote", "Role | Company"):
        assert cleaned == ""


def test_the_intro_uses_the_short_role():
    intro = llm.draft_intro(_ex("AI Engineer (New Grads & Principal/Distinguished)"), EN_POST, _profile_with_intro())
    assert intro.startswith("I'm applying for the AI Engineer position. ")
