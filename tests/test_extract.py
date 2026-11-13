"""raw_text -> ExtractResult heuristics (pure)."""

import pytest

from job_hunter.extract import extract


def test_detects_stack_tokens():
    r = extract("We use Python, FastAPI, Claude API and build RAG agents.", "@c")
    assert "python" in r.stack
    assert "fastapi" in r.stack
    assert "claude" in r.stack
    assert "rag" in r.stack
    assert "agent" in r.stack


def test_remote_true_when_only_remote():
    r = extract("Fully remote position, work from anywhere.", "@c")
    assert r.remote is True


def test_office_false_when_only_office():
    r = extract("Onsite role in our Moscow office.", "@c")
    assert r.remote is False


def test_remote_unknown_when_neither():
    r = extract("Great LLM job, apply now.", "@c")
    assert r.remote is None


def test_salary_range_parsed():
    r = extract("Зарплата 150 000 - 250 000 руб", "@c")
    assert r.salary_min == 150000
    assert r.salary_max == 250000
    assert r.currency == "RUB"


def test_salary_k_suffix_and_currency_symbol():
    r = extract("Salary: €3000 per month", "@c")
    assert r.currency == "EUR"
    assert r.salary_min == 3000


def test_seniority_detection():
    assert extract("Senior Python dev", "@c").seniority == "senior"
    assert extract("Ищем middle+ инженера", "@c").seniority == "middle+"


def test_contact_email_maps_to_null_type():
    # Email has no clean dm|form|link enum slot -> contact_type null,
    # but the email address is still captured in contact.
    r = extract("Send CV to jobs@acme.io", "@c")
    assert r.contact_type is None
    assert r.contact == "jobs@acme.io"


def test_contact_telegram_handle():
    r = extract("Contact @hr_acme to apply", "@c")
    assert r.contact_type == "dm"
    assert r.contact == "@hr_acme"


def test_relocation_flag():
    r = extract("Relocation to Poland supported", "@c")
    assert r.relocation is True


def test_title_first_line():
    r = extract("AI Agent Engineer\n\nDetails here", "@c")
    assert r.title == "AI Agent Engineer"


def test_deterministic():
    text = "Remote Senior Python LLM engineer, 200k-300k USD, @hr"
    a = extract(text, "@c").to_json()
    b = extract(text, "@c").to_json()
    assert a == b


# --- DATE-NOT-SALARY (Part 3) ----------------------------------------------


def test_deadline_do_31_maya_is_not_salary():
    r = extract("Откликнуться до 31 мая. Python LLM разработчик, remote.", "@c")
    assert r.salary_min is None
    assert r.salary_max is None


def test_english_apply_by_may_31_not_salary():
    r = extract("AI Engineer. Apply by May 31. Remote. @hr", "@c")
    assert r.salary_min is None
    assert r.salary_max is None


def test_numeric_deadline_not_salary_but_real_salary_kept():
    r = extract("Дедлайн подачи: 15.06.2026. Зарплата 200000 RUB.", "@c")
    assert r.salary_min == 200000
    assert r.salary_max == 200000
    assert r.currency == "RUB"


def test_deadline_plus_real_salary_range():
    r = extract("Вакансия до 30 июня. Оклад 180 000 - 250 000 руб. Remote.", "@c")
    assert r.salary_min == 180000
    assert r.salary_max == 250000


def test_salary_requires_currency_anchor():
    # A bare number with no money context must NOT be read as salary.
    r = extract("Команда из 50 человек. Senior Python developer.", "@c")
    assert r.salary_min is None
    assert r.salary_max is None


# --- FIX 3: contact found ANYWHERE in the body (incl. last line) ------------

_LONG_POST = (
    "Вакансия: AI / Prompt инженер\n\n"
    "Мы строим ассистента на LLM. Стек: Python, RAG, Qdrant, LangChain.\n"
    "Удалённо, релокация возможна.\n\n"
    "Описание задач занимает несколько абзацев текста, чтобы контакт\n"
    "оказался в самом низу длинного поста после всего описания.\n\n"
    "Подробнее об условиях и команде расскажем на собеседовании.\n\n"
    "{tail}"
)


def test_contact_email_on_last_line_of_long_post():
    r = extract(_LONG_POST.format(tail="Контакты: info@examplecorp.example"), "@jobschan",
                "https://t.me/jobschan/5")
    assert r.contact == "info@examplecorp.example"
    # email -> no clean enum slot, contact_type null (documented mapping).
    assert r.contact_type is None
    # source channel must NOT be used as contact.
    assert r.contact != "@jobschan"


def test_contact_handle_at_bottom_of_long_post():
    r = extract(_LONG_POST.format(tail="Писать: @examplecorp_hr"), "@jobschan",
                "https://t.me/jobschan/5")
    assert r.contact == "@examplecorp_hr"
    assert r.contact_type == "dm"


def test_contact_apply_url_at_bottom():
    r = extract(_LONG_POST.format(tail="Откликнуться: https://examplecorp.example/apply"),
                "@jobschan", "https://t.me/jobschan/5")
    assert r.contact == "https://examplecorp.example/apply"
    assert r.contact_type == "form"


def test_heuristic_does_not_use_source_channel_as_contact():
    # The only @handle present is the channel itself -> contact stays empty.
    text = (
        "AI Engineer. Remote.\n"
        "Подробности в нашем канале @jobschan.\n"
    )
    r = extract(text, "@jobschan", "https://t.me/jobschan/5")
    assert r.contact is None
    assert r.contact_type is None


def test_heuristic_channel_handle_skipped_but_real_handle_kept():
    text = (
        "AI Engineer @jobschan (наш канал).\n"
        "Отклик присылайте @real_hr.\n"
    )
    r = extract(text, "@jobschan", "https://t.me/jobschan/5")
    assert r.contact == "@real_hr"
    assert r.contact_type == "dm"


# --- HASHTAGS + benefits-block remote resolution ----------------------------


def test_hashtag_remote_resolves_yes_anywhere():
    # #УдаленкаРФ in a trailing hashtag row -> remote == True.
    text = (
        "Вакансия: Python разработчик.\n"
        "Описание задач и требований.\n\n"
        "#УдаленкаРФ #python #вакансия\n"
    )
    r = extract(text, "@c")
    assert r.remote is True


def test_remote_phrase_in_benefits_block_not_top_line():
    # "Полная удалёнка" lives in a «Что мы предлагаем» block, not the top.
    text = (
        "AI Engineer\n"
        "Требования: опыт с LLM, Python, RAG.\n"
        "Задачи: строить ассистента.\n\n"
        "Что мы предлагаем:\n"
        "- Полная удалёнка\n"
        "- ДМС и обучение\n"
    )
    r = extract(text, "@c")
    assert r.remote is True


def test_hashtag_location_moscow_and_spb():
    assert extract("AI Engineer\n#Москва #python", "@c").location == "Москва"
    assert extract("AI Engineer\n#Moscow", "@c").location == "Москва"
    assert "Санкт-Петербург" in extract("AI Engineer\n#СПб", "@c").location
    assert "Санкт-Петербург" in extract("AI Engineer\n#Питер", "@c").location


def test_hashtag_seniority():
    assert extract("Dev\n#middle #python", "@c").seniority == "middle"
    assert extract("Dev\n#senior", "@c").seniority == "senior"
    assert extract("Dev\n#миддл", "@c").seniority == "middle"


def test_office_phrase_only_is_false():
    # "в офисе" with no remote signal -> remote == False.
    r = extract("Работа в офисе, дружная команда. Python.", "@c")
    assert r.remote is False


def test_hybrid_representation_remote_true_with_location_tag():
    # Hybrid: remote True (includes remote work) + "(гибрид)" recorded on location.
    r = extract("Формат работы: гибрид. Python LLM инженер.", "@c")
    assert r.remote is True
    assert r.location is not None and "гибрид" in r.location.lower()


def test_hybrid_with_hashtag_location_keeps_both():
    r = extract("Python инженер\nФормат: гибрид\n#Москва", "@c")
    assert r.remote is True
    assert "Москва" in r.location
    assert "гибрид" in r.location.lower()


def test_remote_unknown_stays_none_with_no_signals():
    r = extract("Python developer. Стек: Python, FastAPI.", "@c")
    assert r.remote is None
    assert r.location is None


def test_bottom_contact_still_captured_with_hashtags_and_remote():
    # New remote/hashtag parsing must not break bottom-line contact capture.
    text = (
        "AI / Prompt инженер.\n"
        "Что мы предлагаем:\n- Полная удалёнка\n\n"
        "#УдаленкаРФ #middle #Москва\n"
        "Контакты: info@examplecorp.example"
    )
    r = extract(text, "@jobschan", "https://t.me/jobschan/5")
    assert r.remote is True
    assert r.location == "Москва"
    assert r.seniority == "middle"
    assert r.contact == "info@examplecorp.example"
    assert r.contact_type is None


# --- Tester-added gap tests (Batch 2 validation) ----------------------------


def test_hashtag_regiony_sets_location():
    """#Регионы hashtag -> location='Регионы' (EXTRACT_SYSTEM spec / hashtag map)."""
    r = extract("Python разработчик, хороший стек.\n#Регионы #middle", "@c")
    assert r.location == "Регионы"


def test_hashtag_remote_english_resolves_yes():
    """#Remote (English, lowercase-normalized) hashtag -> remote=True.
    _HASHTAG_REMOTE contains 'remote' (normalized); the EXTRACT_SYSTEM lists
    #Remote as an explicit example. Only #УдаленкаРФ was previously tested.
    """
    r = extract("LLM Engineer. Apply now.\n#Remote #middle", "@c")
    assert r.remote is True


def test_contact_type_link_for_generic_url():
    """A generic (non-apply) URL -> contact_type='link', not 'form'.
    _detect_contact: URLs without 'apply'/'form'/'hh.ru'/'career' in their
    path get contact_type='link'. Only 'form' was previously covered.
    """
    r = extract(
        "AI Engineer. See our website: https://company.io/team",
        "@c",
    )
    # The generic URL should be captured with contact_type 'link'.
    assert r.contact == "https://company.io/team"
    assert r.contact_type == "link"


def test_hybrid_lossiness_known_limitation():
    """KNOWN LIMITATION (documented, not a bug): under the boolean ``remote``
    field, hybrid and fully-remote are indistinguishable on the field value
    alone. The disambiguation lives exclusively in the '(гибрид)' suffix on
    ``location``. This test pins that known limitation so any change that
    inadvertently makes them distinguishable on ``remote`` alone is flagged.
    """
    hybrid = extract("Формат: гибрид. Python LLM.", "@c")
    fully_remote = extract("Полная удалёнка. Python LLM.", "@c")
    # Both return remote=True from the ``remote`` field.
    assert hybrid.remote is True
    assert fully_remote.remote is True
    # They ARE distinguishable only via the location suffix.
    assert hybrid.location is not None and "гибрид" in hybrid.location
    # A fully-remote post has no "(гибрид)" tag (location stays None when no
    # hashtag location is present).
    assert fully_remote.location is None or "гибрид" not in (fully_remote.location or "")


# ---------------------------------------------------------------------------
# B1: "€70k – €100k", "€1,800–€2,500 NET/month" and the currency next to the amount
# (production cases 2026-11-12: a Ruby/Frontend job quoted in euros was stored as 70 000 RUB; a range lost its max)
# ---------------------------------------------------------------------------


from job_hunter import extract as _extract_mod  # noqa: E402


def _salary(text):
    r = extract(text, "ch")
    return r.salary_min, r.salary_max, r.currency


def test_symbol_before_both_amounts_k_suffix_keeps_the_max():
    assert _salary("Salary range: €70k – €100k") == (70000.0, 100000.0, "EUR")
    assert _salary("Salary range: €70k – €120k") == (70000.0, 120000.0, "EUR")


def test_symbol_before_both_amounts_with_thousand_comma_and_net_month_note():
    assert _salary("📍 Limassol 💰 Salary: €1,800–€2,500 NET/month 📩 write us") == (1800.0, 2500.0, "EUR")


def test_symbol_before_first_amount_only():
    assert _salary("Salary: $4,000-6,000 per month") == (4000.0, 6000.0, "USD")
    assert _salary("$120k-$150k/yr") == (120000.0, 150000.0, "USD")


def test_existing_anchor_after_the_range_still_works():
    assert _salary("💰 1800–2200 EUR 📍 office") == (1800.0, 2200.0, "EUR")
    assert _salary("Зарплата 200 000 - 300 000 ₽") == (200000.0, 300000.0, "RUB")


def test_currency_letters_inside_words_are_not_a_currency():
    """'rub' in 'Ruby', 'eur' in 'Europe': not RUB / EUR."""
    assert _salary("Senior Ruby Engineer in Europe")[2] is None
    assert _extract_mod._detect_currency("ruby, scrubbing, rubric") is None
    assert _extract_mod._detect_currency("remote in europe") is None
    assert _extract_mod._detect_currency("pay 100 USD") == "USD"
    assert _extract_mod._detect_currency("оплата 100 руб.") == "RUB"


def test_currency_next_to_the_amount_beats_a_word_that_looks_like_another():
    """A Ruby job quoted in euros: EUR (next to the amount), not RUB from 'Ruby'."""
    assert _salary("Senior Ruby Engineer - Remote Europe. Salary range: €70k – €120k") == (70000.0, 120000.0, "EUR")


def test_no_amount_no_salary():
    assert _salary("Deadline: apply by 31 May") == (None, None, None)


def test_k_suffix_on_the_second_end_applies_to_the_first():
    assert _salary("Salary: $150-230k") == (150000.0, 230000.0, "USD")
    assert _salary("€70 – 100k") == (70000.0, 100000.0, "EUR")
    assert _salary("up to $100-200k USD") == (100000.0, 200000.0, "USD")


def test_k_suffix_is_not_propagated_when_it_would_be_wrong():
    assert _salary("$55 - $75 USD per hour") == (55.0, 75.0, "USD")          # no suffix: hourly stays hourly
    assert _salary("$120k-$150k/yr") == (120000.0, 150000.0, "USD")         # both ends have it
    assert _salary("€1,800–€2,500 NET/month") == (1800.0, 2500.0, "EUR")    # comma thousands, no suffix


def test_russian_do_is_a_range_separator():
    """"Зп: 2 200 до 2 750 €" kept only 2 200 (the max was lost) before."""
    assert _salary("Формат работы: офис. Зп: 2 200 до 2 750 € + KPI") == (2200.0, 2750.0, "EUR")
    assert _salary("Зарплата от 200 000 до 300 000 ₽") == (200000.0, 300000.0, "RUB")


def test_open_ended_amounts_are_still_a_single_figure():
    """A lower bound ("от 180 000 ₽") or a cap ("up to $5000") is read as one figure with its currency."""
    assert _salary("ML Python Developer от 180 000 ₽ за месяц, на руки") == (180000.0, 180000.0, "RUB")
    assert _salary("up to $5000") == (5000.0, 5000.0, "USD")


def test_cyrillic_k_is_thousands():
    assert extract("зп 100-200к", "ch").salary_min == 100000.0
    assert extract("зп 100-200к", "ch").salary_max == 200000.0
    assert extract("оплата 250к ₽", "ch").salary_min == 250000.0
    assert extract("зарплата 1кк ₽", "ch").salary_min == 1000000.0        # "кк" is still a million


# --- B1 (1c): amounts that are not a salary (benefits, equity, funding) -----------------------------------------
# Invented texts only: this repository is public.


@pytest.mark.parametrize("text", [
    "Perks: up to 1390zl wallet for benefits",
    "We just raised $200M Series B and are hiring",
    "Relocation package €5000 for the right person",
    "Sign-on bonus $10,000 after the first month",
    "Equity: $50k in options over four years",
    "Learning budget €1500 per year",
    "Annual stipend 3000 EUR for books",
    "Компенсация обучения до 50 000 ₽ в год",
    "Льготы: до 30 000 ₽ на спорт",
    "Reimbursable up to $800 for a home office",
])
def test_a_non_salary_amount_is_not_read_as_pay(text):
    assert _salary(text)[:2] == (None, None)                 # no amount (the currency field is detected on its own)


def test_a_real_salary_next_to_a_bonus_is_kept():
    assert _salary("Salary 5000-7000 EUR plus bonus up to 10%") == (5000.0, 7000.0, "EUR")
    assert _salary("Зарплата 200 000 - 300 000 ₽ плюс бонус до 50 000 ₽") == (200000.0, 300000.0, "RUB")


def test_the_salary_wins_over_a_bonus_amount_with_a_higher_priority_pattern():
    """The bonus is a range behind a symbol (higher priority than the keyword), the salary sits behind a keyword."""
    assert _salary("Relocation bonus €2000-3000. Salary: 4000 EUR") == (4000.0, 4000.0, "EUR")
    assert _salary("Salary: 4000 EUR; bonus: €500-800") == (4000.0, 4000.0, "EUR")


def test_a_marker_far_from_the_amount_does_not_block_it():
    far = "€3000 net per month. " + "We are a friendly remote team building developer tools for small shops. " + "Also equity."
    assert _salary(far)[0] == 3000.0


def test_the_keyword_that_introduces_an_amount_is_not_a_context_marker():
    assert _salary("Компенсация 200 000 ₽ в месяц") == (200000.0, 200000.0, "RUB")


def test_a_plain_salary_without_markers_is_unchanged():
    assert _salary("от 180 000 ₽ на руки") == (180000.0, 180000.0, "RUB")
    assert _salary("Base salary $150k") == (150000.0, 150000.0, "USD")


def test_an_amount_with_a_marker_but_no_currency_and_no_keyword_stays_unknown():
    assert _salary("Base: 150k + equity")[:2] == (None, None)


def test_extract_leaves_the_salary_empty_for_a_benefit_only_post():
    r = extract("Python developer. Perks: up to 1390zl wallet for benefits, remote.", "ch")
    assert r.salary_min is None and r.salary_max is None
