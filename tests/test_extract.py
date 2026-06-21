"""raw_text -> ExtractResult heuristics (pure)."""

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
    r = extract(_LONG_POST.format(tail="Контакты: info@netbell.ru"), "@jobschan",
                "https://t.me/jobschan/5")
    assert r.contact == "info@netbell.ru"
    # email -> no clean enum slot, contact_type null (documented mapping).
    assert r.contact_type is None
    # source channel must NOT be used as contact.
    assert r.contact != "@jobschan"


def test_contact_handle_at_bottom_of_long_post():
    r = extract(_LONG_POST.format(tail="Писать: @netbell_hr"), "@jobschan",
                "https://t.me/jobschan/5")
    assert r.contact == "@netbell_hr"
    assert r.contact_type == "dm"


def test_contact_apply_url_at_bottom():
    r = extract(_LONG_POST.format(tail="Откликнуться: https://netbell.ru/apply"),
                "@jobschan", "https://t.me/jobschan/5")
    assert r.contact == "https://netbell.ru/apply"
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
        "Контакты: info@netbell.ru"
    )
    r = extract(text, "@jobschan", "https://t.me/jobschan/5")
    assert r.remote is True
    assert r.location == "Москва"
    assert r.seniority == "middle"
    assert r.contact == "info@netbell.ru"
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
