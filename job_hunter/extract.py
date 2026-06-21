"""PURE: heuristic extraction of structured fields from a raw job post.

No I/O, no clock, no DB, no network. This is the deterministic baseline used
when no LLM is available (and as a fallback when the LLM call fails). The
smart LLM extract (T1) lives in ``llm.py`` and reuses this module's schema and
helpers.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .schema_extract import ExtractResult, from_dict

# --- Stack / keyword vocabulary ---------------------------------------------
# Maps a canonical stack token -> regex of synonyms (case-insensitive).
_STACK_PATTERNS = {
    "python": r"\bpython\b",
    "aiogram": r"\baiogram\b",
    "telethon": r"\btelethon\b",
    "claude": r"\bclaude\b|\banthropic\b",
    "openai": r"\bopenai\b|\bgpt-?4\b|\bchatgpt\b",
    "llm": r"\bllm\b|\bllms\b|large language model",
    "rag": r"\brag\b|retrieval[- ]augmented",
    "fastapi": r"\bfastapi\b",
    "docker": r"\bdocker\b",
    "langchain": r"\blangchain\b",
    "kubernetes": r"\bkubernetes\b|\bk8s\b",
    "postgres": r"\bpostgres\b|\bpostgresql\b",
    "pytorch": r"\bpytorch\b",
    "tensorflow": r"\btensorflow\b",
    "prompt": r"prompt engineering|\bprompting\b|\bprompt\b",
    "eval": r"\bevals?\b|evaluation pipeline",
    "agent": r"\bai agent\b|\bagentic\b|\bagents?\b",
}

# Work-format phrases scanned ANYWHERE in the body (top lines, a benefits /
# «Что мы предлагаем» block, or the tail) — not just the first lines.
# "Полная удалёнка", "удалёнка", "remote", "удаленно", "fully remote", "wfh".
_REMOTE_RE = re.compile(
    r"\bremote\b|удал[её]н|удал[её]нн|wfh|work from home|fully remote", re.I
)
# Negators that, when sitting immediately before a remote token, FLIP its
# meaning ("без удалёнки", "no remote", "не удалённо"). Such a match must NOT
# count as a positive remote signal. We allow an optional run of separators
# (spaces / dashes / punctuation) between the negator and the token.
_REMOTE_NEGATOR_RE = re.compile(
    r"(?:\bбез\b|\bno\b|\bне\b|\bwithout\b)[\s\-–—,:;'\"]*$", re.I
)
_OFFICE_RE = re.compile(r"\boffice\b|офис|on-?site|on site|в офисе", re.I)
# Hybrid format ("гибрид"/"hybrid"): includes remote work, so remote -> True,
# but the nuance is recorded in the location field (see _HYBRID_TAG).
_HYBRID_RE = re.compile(r"гибрид|\bhybrid\b", re.I)
_RELOCATION_RE = re.compile(r"relocat|релокац|переезд", re.I)

# Suffix appended to the location field to record a hybrid work format without
# adding a schema column (remote stays boolean: True for hybrid since it
# includes remote work). LOSSY note in REMOTE REPRESENTATION below.
_HYBRID_TAG = "(гибрид)"

# --- Hashtag vocabulary (scanned across the FULL post) ----------------------
# Hashtags appear anywhere in a post (often a trailing #УдаленкаРФ #middle row)
# and carry strong, explicit signals. Matching is case-insensitive and tolerant
# of the ё/е variation (handled in _normalize_tag). Keys here are already
# normalized (lowercase, ё->е, '#' stripped) so the map stays easy to extend.
_HASHTAG_REMOTE = {
    "удаленкарф", "удаленка", "remote", "удаленно", "удаленная", "удаленнаяработа",
}
# Normalized hashtag -> canonical location string.
_HASHTAG_LOCATION = {
    "москва": "Москва",
    "moscow": "Москва",
    "мск": "Москва",
    "спб": "Санкт-Петербург",
    "питер": "Санкт-Петербург",
    "spb": "Санкт-Петербург",
    "saintpetersburg": "Санкт-Петербург",
    "регионы": "Регионы",
}
# Normalized hashtag -> canonical seniority label (matches _SENIORITY_PATTERNS).
_HASHTAG_SENIORITY = {
    "junior": "junior",
    "джуниор": "junior",
    "джун": "junior",
    "middle": "middle",
    "миддл": "middle",
    "мидл": "middle",
    "middleplus": "middle+",
    "senior": "senior",
    "синьор": "senior",
    "сеньор": "senior",
    "lead": "lead",
    "тимлид": "lead",
    "техлид": "lead",
}

_HASHTAG_RE = re.compile(r"#([A-Za-zА-Яа-яЁё0-9_]+)")

# --- Company label line -----------------------------------------------------
# An explicit "Компания: X" / "Company: X" / "Работодатель: X" label line. The
# value is the rest of the line after the colon, trimmed of surrounding markup.
# Anchored to a line start so a stray "компания" inside prose is not captured.
_COMPANY_LABEL_RE = re.compile(
    r"^[\s>*_•\-]*(?:компани[яи]|company|работодател[ья]|employer)\s*[:\-–—]\s*(.+)$",
    re.I | re.M,
)


def _detect_company(text: str) -> Optional[str]:
    """Capture an explicit 'Компания: X' / 'Company: X' label line. PURE.

    Returns the cleaned company name (e.g. 'Нетбелл' from 'Компания: Нетбелл'),
    or None when no such labelled line is present. Only the labelled form is
    trusted here -- free-form company guessing is left to the LLM extractor.
    """
    m = _COMPANY_LABEL_RE.search(text or "")
    if not m:
        return None
    value = m.group(1).strip().strip("*_`\"' ").strip()
    # Drop a trailing hashtag cluster or markup that sometimes follows the name.
    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    return value or None

# --- Benefits / conditions vocabulary (DISPLAY ONLY — zero scoring impact) ---
# Canonical benefit -> regex of trigger phrases (RU + EN), scanned across the
# WHOLE post (a «Что мы предлагаем» / «We offer» block, hashtags, or the body).
# Each canonical benefit carries BOTH a Russian and an English LABEL; the label
# rendered is chosen by the post's language (see ``_BENEFIT_LABELS`` and
# ``_is_cyrillic``) so the card reads naturally. Easy to extend: add a row.
#
# LABELING CHOICE: labels are stored in the POST'S language. For a Russian post
# we emit the RU label, for an English post the EN label. This keeps the card
# («Условия: …») in one language. The match is language-agnostic (RU+EN
# triggers), only the OUTPUT label depends on the post language.
_BENEFIT_PATTERNS = [
    ("health_insurance", r"\bдмс\b|медстрахов\w*|мед\.?\s*страхов\w*|страховк\w*|health insurance|\bmedical\b|health cover\w*"),
    ("remote_perk", r"полн\w*\s+удал[её]нк\w*|удал[её]нк\w*|remote work|fully remote|work from home|\bwfh\b"),
    ("relocation", r"релокац\w*|помощ\w*\s+с\s+переезд\w*|переезд\w*|relocation|relocation package|relocation support"),
    ("visa_sponsorship", r"визов\w*\s+спонсор\w*|спонсор\w*\s+виз\w*|помощ\w*\s+с\s+виз\w*|visa sponsorship|visa support|work permit|blue card|blue-card"),
    ("learning", r"обучен\w*|курс\w*|оплат\w*\s+обучен\w*|конференц\w*|learning budget|training budget|education budget|courses|conferences"),
    ("equipment", r"техник\w*|оборудован\w*|\bmacbook\b|ноутбук\w*|equipment|hardware|laptop"),
    ("bonus", r"бонус\w*|преми\w*|\bkpi\b|bonus(?:es)?|\brsu\b|stock options|equity"),
    ("paid_vacation", r"оплачива\w*\s+отпуск\w*|отпуск\w*|paid (?:vacation|leave|time off)|\bpto\b|paid holidays"),
    ("sport", r"\bспорт\w*|фитнес\w*|спортзал\w*|gym|sport(?:s)?\s+(?:compensation|membership)|fitness"),
    ("language_classes", r"английск\w*\s+язык\w*|уроки\s+английск\w*|курс\w*\s+английск\w*|english (?:classes|lessons|courses)|language (?:classes|lessons|courses)"),
    ("flexible_hours", r"гибк\w*\s+график\w*|гибк\w*\s+час\w*|свободн\w*\s+график\w*|flexible (?:hours|schedule|working hours)|flexible schedule"),
]

# canonical -> (RU label, EN label). Both are display strings for the card.
_BENEFIT_LABELS = {
    "health_insurance": ("ДМС / медстраховка", "Health insurance"),
    "remote_perk": ("Удалёнка", "Remote work"),
    "relocation": ("Помощь с релокацией", "Relocation support"),
    "visa_sponsorship": ("Визовый спонсор", "Visa sponsorship"),
    "learning": ("Обучение", "Learning budget"),
    "equipment": ("Техника / оборудование", "Equipment"),
    "bonus": ("Бонусы / премии", "Bonuses"),
    "paid_vacation": ("Оплачиваемый отпуск", "Paid vacation"),
    "sport": ("Спорт / фитнес", "Sports / fitness"),
    "language_classes": ("Занятия английским", "Language classes"),
    "flexible_hours": ("Гибкий график", "Flexible hours"),
}

_BENEFIT_COMPILED = [(canon, re.compile(pat, re.I)) for canon, pat in _BENEFIT_PATTERNS]

_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")


def _is_cyrillic(text: str) -> bool:
    """True when the text contains Cyrillic letters. PURE (local heuristic).

    Kept local so extract.py stays pure / dependency-free (mirrors bot.is_russian).
    """
    return bool(_CYRILLIC_RE.search(text or ""))


def _detect_benefits(text: str) -> List[str]:
    """Return a de-duplicated list of canonical benefit LABELS found. PURE.

    DISPLAY ONLY — never feeds scoring. Labels are emitted in the post's
    language (RU label for a Cyrillic post, else EN). Order follows the
    declaration order of ``_BENEFIT_PATTERNS`` for stable, deterministic output.
    """
    russian = _is_cyrillic(text)
    found: List[str] = []
    seen = set()
    for canon, rx in _BENEFIT_COMPILED:
        if canon in seen:
            continue
        if rx.search(text):
            ru, en = _BENEFIT_LABELS[canon]
            found.append(ru if russian else en)
            seen.add(canon)
    return found

_SENIORITY_PATTERNS = [
    ("lead", r"\blead\b|\bteam ?lead\b|тимлид|тех ?лид"),
    ("senior", r"\bsenior\b|\bsr\.?\b|синьор|сеньор"),
    ("middle+", r"middle\+|миддл\+|мидл\+"),
    ("middle", r"\bmiddle\b|\bmid\b|миддл|мидл"),
    ("junior", r"\bjunior\b|\bjr\.?\b|джун"),
]

# Currency symbols / codes -> canonical ISO code.
_CURRENCY_MAP = {
    "₽": "RUB", "руб": "RUB", "rub": "RUB", "р.": "RUB",
    "$": "USD", "usd": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
    "₸": "KZT", "kzt": "KZT",
}

# RFC-ish email, a Telegram @handle, and application URLs (http(s) or t.me).
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_TG_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{3,}")
_URL_RE = re.compile(r"(?:https?://\S+|t\.me/\S+)")


def _detect_stack(text: str) -> List[str]:
    found: List[str] = []
    for token, pat in _STACK_PATTERNS.items():
        if re.search(pat, text, re.I):
            found.append(token)
    return found


def _detect_seniority(text: str) -> Optional[str]:
    for label, pat in _SENIORITY_PATTERNS:
        if re.search(pat, text, re.I):
            return label
    return None


def _normalize_tag(tag: str) -> str:
    """Lowercase + collapse the ё/е variation for hashtag lookup. PURE."""
    return tag.lower().replace("ё", "е")


def _parse_hashtags(text: str) -> List[str]:
    """Return normalized hashtag tokens found ANYWHERE in the post. PURE."""
    return [_normalize_tag(m.group(1)) for m in _HASHTAG_RE.finditer(text)]


def _hashtag_remote(tags: List[str]) -> bool:
    return any(t in _HASHTAG_REMOTE for t in tags)


def _hashtag_location(tags: List[str]) -> Optional[str]:
    for t in tags:
        if t in _HASHTAG_LOCATION:
            return _HASHTAG_LOCATION[t]
    return None


def _hashtag_seniority(tags: List[str]) -> Optional[str]:
    for t in tags:
        if t in _HASHTAG_SENIORITY:
            return _HASHTAG_SENIORITY[t]
    return None


def _has_remote_signal(text: str) -> bool:
    """True iff ``text`` carries a POSITIVE remote phrase. PURE.

    Scans every ``_REMOTE_RE`` match and discards any whose text is immediately
    preceded by a negator ("без удалёнки", "no remote", "не удалённо"): such a
    phrase means the opposite, so it must not count as a remote signal. A single
    legitimate remote match anywhere is enough to return True.
    """
    for m in _REMOTE_RE.finditer(text):
        if not _REMOTE_NEGATOR_RE.search(text, 0, m.start()):
            return True
    return False


def _detect_remote(text: str, hashtag_remote: bool = False) -> Tuple[Optional[bool], bool]:
    """Resolve remote/hybrid/office from hashtags + body phrases ANYWHERE.

    The regexes use ``.search`` over the WHOLE text, so a format phrase in a
    «Что мы предлагаем» / benefits block or the tail counts the same as a top
    line. Hashtag remote signal (e.g. #УдаленкаРФ) is folded in via
    ``hashtag_remote``.

    REMOTE REPRESENTATION (documented choice — no new schema column):
      - fully remote  -> (True, False)
      - office only   -> (False, False)
      - hybrid        -> (True, True)   # remote=True (hybrid includes remote);
                         the 2nd flag tells extract() to tag location "(гибрид)".
      - unknown       -> (None, False)
    LOSSY NOTE: under the boolean ``remote`` field, hybrid and fully-remote are
    indistinguishable on the field itself; hybrid is disambiguated only by the
    "(гибрид)" suffix on ``location``.

    Returns (remote, is_hybrid).
    """
    has_remote = _has_remote_signal(text) or hashtag_remote
    has_office = bool(_OFFICE_RE.search(text))
    has_hybrid = bool(_HYBRID_RE.search(text))

    if has_hybrid:
        return True, True
    if has_remote and not has_office:
        return True, False
    if has_office and not has_remote:
        return False, False
    if has_remote and has_office:
        # both formats offered -> treat as hybrid (remote is available).
        return True, True
    return None, False


def _detect_currency(text: str) -> Optional[str]:
    low = text.lower()
    for token, code in _CURRENCY_MAP.items():
        if token in low:
            return code
    return None


def _parse_amount(raw: str) -> Optional[float]:
    """Parse a numeric chunk like '150 000', '150k', '3.000', '290k' -> float."""
    raw = raw.strip().lower().replace(",", "").replace(" ", "").replace(" ", "")
    mult = 1.0
    if raw.endswith("k"):
        mult = 1_000.0
        raw = raw[:-1]
    elif raw.endswith("m") or raw.endswith("кк"):
        mult = 1_000_000.0
        raw = raw.rstrip("mкк")
    # Drop any trailing currency letters/symbols.
    raw = re.sub(r"[^\d.]", "", raw)
    if not raw:
        return None
    try:
        return float(raw) * mult
    except ValueError:
        return None


# Month names (RU + EN, full + common abbreviations) used to recognize and
# strip DATE/DEADLINE phrases so they are never misread as salary.
_MONTHS = (
    r"янв\w*|февр?\w*|март?\w*|апр\w*|ма[йя]|июн\w*|июл\w*|авг\w*|сент?\w*|"
    r"окт\w*|нояб?\w*|дек\w*|"
    r"jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*"
)

# Date / deadline phrases removed before salary parsing. Their numbers must NOT
# become salary: "до 31 мая", "по 30 июня", "until May 31", "15.06.2026", ...
_DATE_PHRASE_RES = [
    re.compile(rf"\b(?:до|по|until|till|by|before|дедлайн|deadline)\b[^.\n]*?\b(?:{_MONTHS})\b", re.I),
    re.compile(rf"\b\d{{1,2}}\s*(?:{_MONTHS})\b", re.I),
    re.compile(rf"\b(?:{_MONTHS})\s*\d{{1,2}}\b", re.I),
    re.compile(r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b(?:до|по|until|by)\b\s*\d{4}\s*(?:года|г\.?|year)?\b", re.I),
]

# A money amount: digits (separators ok) with optional k/m suffix. Salary
# detection REQUIRES a currency anchor adjacent, which is what stops bare date
# numbers ("31") from being read as salary.
_CCY_ANCHOR = r"₽|руб\w*|\brub\b|\$|\busd\b|€|\beur\b|£|\bgbp\b|₸|\bkzt\b|\bk\b|\bтыс\w*"
_AMOUNT = r"\d[\d\s.,]*\s*[kкmм]{0,2}"

# Range with a currency/money anchor in the phrase.
_SALARY_RANGE_RE = re.compile(
    rf"({_AMOUNT})\s*(?:[-–—]|to)\s*({_AMOUNT})\s*(?:{_CCY_ANCHOR})",
    re.I,
)
# Money keyword/symbol introducing an amount (optionally a range).
_SALARY_KEYWORD_RE = re.compile(
    rf"(?:зп|salary|зарплат\w*|оклад|вилка|компенсаци\w*|pay|💰|от|from|up to)\s*[:\-]?\s*"
    rf"({_AMOUNT})(?:\s*[-–—to]+\s*({_AMOUNT}))?\s*(?:{_CCY_ANCHOR})?",
    re.I,
)
# Currency symbol/code directly attached to an amount: "€3000", "$4,000", "150000₽".
_SALARY_SYMBOL_RE = re.compile(
    rf"(?:₽|\$|€|£|₸)\s*({_AMOUNT})|({_AMOUNT})\s*(?:₽|руб\w*|\brub\b|\busd\b|\beur\b|₸|\bkzt\b)",
    re.I,
)


def _strip_dates(text: str) -> str:
    """Blank out date/deadline phrases so their numbers can't become salary."""
    for rx in _DATE_PHRASE_RES:
        text = rx.sub(" ", text)
    return text


def _ordered(lo: Optional[float], hi: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    if lo is not None and hi is not None and lo > hi:
        return hi, lo
    return lo, hi


def _detect_salary(text: str) -> Tuple[Optional[float], Optional[float]]:
    """Extract a money range, anchored to currency context and ignoring dates.

    A bare number with no currency/money anchor (e.g. a deadline '31') is NOT
    treated as salary.
    """
    text = _strip_dates(text)

    m = _SALARY_RANGE_RE.search(text)
    if m:
        return _ordered(_parse_amount(m.group(1)), _parse_amount(m.group(2)))

    m = _SALARY_KEYWORD_RE.search(text)
    if m:
        lo = _parse_amount(m.group(1))
        hi = _parse_amount(m.group(2)) if m.group(2) else None
        if hi is None:
            return lo, lo
        return _ordered(lo, hi)

    m = _SALARY_SYMBOL_RE.search(text)
    if m:
        val = _parse_amount(m.group(1) or m.group(2))
        return val, val

    return None, None


def _norm_channel(value: Optional[str]) -> Optional[str]:
    """Normalize a channel/handle for the channel-leak guard. PURE."""
    if not value:
        return None
    c = value.strip().lower().lstrip("@").rstrip("/")
    c = re.sub(r"^https?://t\.me/(s/)?", "", c)
    c = c.rstrip("/")
    return c or None


def _is_channel(candidate: str, source_channel: str, source_link: Optional[str]) -> bool:
    """True when ``candidate`` is actually the source channel (a leak). PURE."""
    cand = _norm_channel(candidate)
    if not cand:
        return False
    chan = _norm_channel(source_channel)
    if chan and (cand == chan or cand == f"t.me/{chan}"):
        return True
    if source_link:
        link = source_link.strip().lower().rstrip("/")
        if cand == link or cand == _norm_channel(source_link):
            return True
    return False


def _detect_contact(
    text: str,
    source_channel: str = "",
    source_link: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Find the application contact ANYWHERE in the full text. PURE.

    Scans the WHOLE post (including the last lines) for an email, a Telegram
    @handle, or an apply URL. The source channel is never returned as the
    contact. Returns (contact_type, contact):
      - email  -> (None, "<email>")   # email has no dm|form|link enum slot
      - @handle-> ("dm", "@handle")
      - URL    -> ("form"|"link", "<url>")
    """
    # Email anywhere wins as the most explicit application contact.
    email = _EMAIL_RE.search(text)
    if email:
        # email has no clean dm|form|link slot -> contact_type null per schema.
        return None, email.group(0)

    for tg in _TG_RE.finditer(text):
        handle = tg.group(0)
        if not _is_channel(handle, source_channel, source_link):
            return "dm", handle

    for url in _URL_RE.finditer(text):
        candidate = url.group(0).rstrip(".,);")
        if _is_channel(candidate, source_channel, source_link):
            continue
        low = candidate.lower()
        ctype = "form" if any(k in low for k in ("form", "apply", "hh.ru", "career")) else "link"
        return ctype, candidate
    return None, None


def _detect_title(text: str) -> str:
    """First non-empty line, trimmed of emoji/markup, as a title guess."""
    for line in text.splitlines():
        stripped = line.strip().strip("#*_- ").strip()
        if stripped:
            return stripped[:120]
    return text.strip()[:120] or "(untitled)"


def extract(
    raw_text: str,
    source_channel: str = "",
    source_link: Optional[str] = None,
) -> ExtractResult:
    """Heuristically parse ``raw_text`` into an ExtractResult.

    Pure and deterministic. Missing/unknown values are None (lists empty).
    """
    text = raw_text or ""
    salary_min, salary_max = _detect_salary(text)
    contact_type, contact = _detect_contact(text, source_channel, source_link)

    relocation: Optional[bool] = True if _RELOCATION_RE.search(text) else None

    # Hashtags carry explicit remote/location/seniority signals anywhere in the
    # post (commonly a trailing "#УдаленкаРФ #middle #Москва" row).
    tags = _parse_hashtags(text)

    remote, is_hybrid = _detect_remote(text, hashtag_remote=_hashtag_remote(tags))

    # Body-phrase seniority wins; hashtag seniority is a fallback.
    seniority = _detect_seniority(text) or _hashtag_seniority(tags)

    # Location comes only from hashtags in the heuristic path (no body parser).
    location: Optional[str] = _hashtag_location(tags)
    # Record the hybrid nuance on location without breaking the boolean schema.
    if is_hybrid:
        if location:
            location = f"{location} {_HYBRID_TAG}"
        else:
            location = _HYBRID_TAG

    return ExtractResult(
        title=_detect_title(text),
        source_channel=source_channel,
        company=_detect_company(text),
        stack=_detect_stack(text),
        seniority=seniority,
        salary_min=salary_min,
        salary_max=salary_max,
        currency=_detect_currency(text),
        remote=remote,
        relocation=relocation,
        location=location,
        contact_type=contact_type,
        contact=contact,
        source_link=source_link,
        relevance_score=None,
        reasons=[],
        benefits=_detect_benefits(text),
    )


# --- RECONCILE: make deterministic resolvers authoritative over the LLM ------
# WHY THIS EXISTS (integration fix):
# In the LIVE path T1 uses the LLM (Haiku) extractor and takes its JSON as the
# ExtractResult. The heuristic resolvers in this module were ONLY used as the
# offline fallback, so hashtag/company/benefit/contact signals that Haiku missed
# (e.g. #УдаленкаРФ -> remote, #middle -> seniority, "Компания: Нетбелл",
# the «Что мы предлагаем» benefits, the bottom-line contact info@netbell.ru)
# never reached the card. ``reconcile`` runs a deterministic ENRICH/RECONCILE
# pass over the SAME raw_text AFTER the LLM step so those fields are guaranteed.
#
# MERGE PRECEDENCE (documented, per field):
#   - remote:     precedence is DIRECTIONAL — the heuristic may only UPGRADE,
#                 never downgrade, a definite LLM value:
#                   (a) a POSITIVE remote signal (h_remote is True: #УдаленкаРФ /
#                       «удалёнка» / remote / hybrid) WINS -> remote=True. This
#                       can flip a LLM False/None up to True (the netbell fix).
#                   (b) when the LLM value is NULL, the heuristic FILLS it with
#                       whatever it found: True for a remote signal, False for an
#                       office-only signal (unknown -> resolved).
#                   (c) a non-null LLM remote is NEVER clobbered by a weak/
#                       downward heuristic guess: an office-only (h_remote is
#                       False) or unknown (None) heuristic read leaves a definite
#                       LLM True/False exactly as the LLM produced it.
#   - seniority:  hashtag/body seniority WINS when the LLM left it null.
#                 (When the LLM gave a value we keep the LLM's -- it reads the
#                 full body, e.g. "Senior" in prose the hashtag map may miss.)
#   - company:    an explicit "Компания: X" label WINS over the LLM (the label
#                 is unambiguous; the LLM sometimes guesses or omits it).
#   - benefits:   the deterministic benefit list WINS when the LLM returned an
#                 empty/whitespace list; otherwise the LLM list is kept.
#   - contact:    a body contact (email / @handle / apply-URL) WINS when the LLM
#                 left contact null OR filled it with the source channel (leak).
#                 The channel-leak guard still applies to the heuristic result.
#   - location:   filled from hashtags only when the LLM left it null.
# Everything else (title, stack, salary, currency, relocation) stays as the LLM
# produced it -- those are not in the heuristic "confident" set here.


def reconcile(
    llm_result: ExtractResult,
    raw_text: str,
    source_channel: str = "",
    source_link: Optional[str] = None,
) -> ExtractResult:
    """Overlay deterministic, high-confidence signals onto an LLM ExtractResult.

    PURE. Returns a NEW ExtractResult (the input is not mutated). See the
    MERGE PRECEDENCE note above for the per-field rules.
    """
    text = raw_text or ""
    tags = _parse_hashtags(text)

    # Run the same deterministic resolvers used by the heuristic extractor.
    h_remote, h_hybrid = _detect_remote(text, hashtag_remote=_hashtag_remote(tags))
    h_seniority = _detect_seniority(text) or _hashtag_seniority(tags)
    h_company = _detect_company(text)
    h_benefits = _detect_benefits(text)
    h_contact_type, h_contact = _detect_contact(text, source_channel, source_link)
    h_location = _hashtag_location(tags)

    # Start from the LLM result's fields.
    d = llm_result.to_dict()

    # --- remote: directional merge (upgrade only; never downgrade) ------------
    # (a) a positive remote signal (h_remote is True) WINS -> True (netbell).
    # (b) a null LLM value is FILLED by any heuristic read (True or False).
    # (c) a definite LLM value is otherwise left untouched: an office-only
    #     (h_remote is False) or unknown (None) heuristic never clobbers it.
    if h_remote is True:
        d["remote"] = True
    elif d.get("remote") is None and h_remote is not None:
        d["remote"] = h_remote

    # --- seniority: hashtag/body fallback fills a null LLM value ---------------
    if not d.get("seniority") and h_seniority:
        d["seniority"] = h_seniority

    # --- company: explicit "Компания: X" label wins ---------------------------
    if h_company:
        d["company"] = h_company

    # --- benefits: deterministic list wins when the LLM gave none -------------
    llm_benefits = [b for b in (d.get("benefits") or []) if str(b).strip()]
    if not llm_benefits and h_benefits:
        d["benefits"] = h_benefits

    # --- contact: body contact wins when the LLM left null or leaked channel --
    llm_contact = d.get("contact")
    llm_contact_is_channel = bool(llm_contact) and _is_channel(
        llm_contact, source_channel, source_link
    )
    if (not llm_contact or llm_contact_is_channel) and h_contact:
        d["contact"] = h_contact
        d["contact_type"] = h_contact_type
    elif llm_contact_is_channel:
        # No heuristic replacement available, but the LLM value is a leak: drop.
        d["contact"] = None
        d["contact_type"] = None

    # --- location: fill from hashtags only when the LLM left it null ----------
    if not d.get("location") and h_location:
        loc = h_location
        if h_hybrid:
            loc = f"{loc} {_HYBRID_TAG}"
        d["location"] = loc
    elif not d.get("location") and h_hybrid:
        d["location"] = _HYBRID_TAG

    return from_dict(d)
