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
    assert out.startswith("Добрый день!\n\n")
    assert not any(d in out for d in LONG_DASHES)
    assert out.endswith("GitHub: github.com/x\nСайт: example.test")


def test_signature_block_is_github_plus_the_profile_line():
    assert _profile_with_site().draft_signature.block() == "GitHub: github.com/x\nСайт: example.test"


# --- the prompt ------------------------------------------------------------------------------------------


def test_prompt_carries_the_style_and_greeting_rules():
    s = llm.build_draft_system(example_profile())
    assert "GREETING (MANDATORY)" in s and "«Добрый день!»" in s and "«Hello,»" in s
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


def test_user_prompt_names_the_exact_greeting_per_language():
    ex = ExtractResult(title="t", source_channel="ch", company=None)
    assert "«Добрый день!»" in llm.build_draft_prompt(ex, RU_POST)
    assert "«Hello,»" in llm.build_draft_prompt(ex, EN_POST)


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
