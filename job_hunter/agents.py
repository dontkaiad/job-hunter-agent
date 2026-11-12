"""Agent(LLM) steps: research (T10) and draft (T11).

DESIGN.md §6 listed these as deferred pass-through stubs; in this build they are
REAL LLM-backed implementations. They take an injected LLMClient so the calling
pipeline stays testable with mocks.

Results are stored back into the item's extracted_json under reserved keys
('research', 'draft') so no schema/table change is needed.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from . import llm, research_fetch
from .llm import LLMClient
from .profile import Profile, example_profile
from .schema_extract import ExtractResult

# Deterministic links appended to EVERY draft. The отклик is sent MANUALLY by
# the operator (no auto-send), and the candidate's proof of skill is her GitHub
# + резюме. We do not trust the LLM to emit these reliably (it tends to drop or
# "slop" them away, or fabricate a fake URL), so they are appended verbatim
# AFTER generation. The actual link + placeholder come from the loaded PROFILE
# (config/profile.*.yaml), NOT hardcoded here. The module-level constants below
# are rendered from the GENERIC example profile so this module carries NO
# personal data; live runs pass the loaded local profile via ``draft(...,
# profile=...)``. GITHUB is a literal URL; RESUME is a literal PLACEHOLDER the
# operator swaps for the real link at send time (we never invent a resume URL).
_EXAMPLE_SIG = example_profile().draft_signature
GITHUB_LINK = _EXAMPLE_SIG.github
RESUME_PLACEHOLDER = _EXAMPLE_SIG.resume
_SIGNATURE_BLOCK = _EXAMPLE_SIG.block()


def signature_block(profile: Optional[Profile] = None) -> str:
    """The GitHub + резюме signature block for ``profile``. PURE.

    Defaults to the GENERIC example signature when no profile is given.
    """
    sig = (profile or example_profile()).draft_signature
    return sig.block()


def append_draft_signature(draft_text: str, profile: Optional[Profile] = None) -> str:
    """Append the deterministic GitHub + резюме signature to a draft. PURE.

    Guarantees both literal references are present even if the LLM omitted or
    mangled them. Idempotent: checks the tail of the body for both the github
    link and resume placeholder — tolerates whitespace variations (LLMs sometimes
    emit the signature with \\n\\n between lines instead of \\n, causing exact
    endswith() to miss and duplicate the block). The GitHub link and резюме
    placeholder are taken from the loaded profile (generic example when None) —
    no real resume URL is ever fabricated here.
    """
    sig = (profile or example_profile()).draft_signature
    block = sig.block()
    body = (draft_text or "").rstrip()
    # Idempotency: scan the tail (2× block length) for both signature tokens so
    # minor whitespace differences don't trigger a duplicate append.
    tail_len = max(len(block) * 2, 120)
    tail = body[-tail_len:]
    if sig.github in tail and sig.resume in tail:
        return body
    if not body:
        return block
    return f"{body}\n\n{block}"


# --- Deterministic polish of the generated body (the prompt asks; the code guarantees) -------------------
# The model is told never to write long / medium dashes and to open with a fixed greeting, but models drift,
# so both are enforced here AFTER generation, before the signature is appended.

_DASHES = "\u2014\u2013"                      # em dash and en dash
_RANGE_DASH_RE = re.compile(rf"(?<=\d)\s*[{_DASHES}]\s*(?=\d)")        # 2022—2024, 10 – 15
_LEADING_DASH_RE = re.compile(rf"(?m)^[ \t]*[{_DASHES}][ \t]*")           # a dash used as a list / dialogue marker
_DASH_RE = re.compile(rf"[ \t]*[{_DASHES}][ \t]*")


def strip_long_dashes(text: str) -> str:
    """Replace em / en dashes: digit ranges become a plain hyphen without spaces ("2022-2024"), a leading
    dash (list marker) is dropped, any other dash becomes a comma. A spaced hyphen is never produced. PURE."""
    if not text or not any(d in text for d in _DASHES):
        return text
    out = _RANGE_DASH_RE.sub("-", text)
    out = _LEADING_DASH_RE.sub("", out)
    out = _DASH_RE.sub(", ", out)
    # tidy what the substitution can leave behind: ", ," / ",." / ", !" / a comma at a line end or before a newline
    out = re.sub(r",(\s*,)+", ",", out)
    out = re.sub(r"\s+,", ",", out)
    out = re.sub(r",\s*([.!?;:])", r"\1", out)
    out = re.sub(r",[ \t]*(?=\n)", ".", out)
    out = re.sub(r",[ \t]*$", ".", out)
    return out


GREETING_RU = "Добрый день!"
GREETING_EN = "Hello,"
_GREETING_WORDS = (r"(?:добрый\s+(?:день|вечер|утро)|доброго\s+времени\s+суток|здравствуйте|здравствуй|привет(?:ствую)?|"
                   r"hello|hi|hey|dear|greetings|good\s+(?:morning|afternoon|evening))\b")
# a line that IS a greeting (maybe with an address: "Добрый день, команда X!", "Dear Hiring Manager,")
# (it ends with "!" or ",", or has no end mark and is very short; a line ending with a full stop is content)
_GREETING_LINE_RE = re.compile(
    rf"^[ \t]*{_GREETING_WORDS}(?:[^\n!.?]{{0,50}}[,!]|(?:[ \t]+\S+){{0,2}})[ \t]*$", re.I)
# a greeting at the start of a line that goes on ("Здравствуйте! Меня заинтересовала ..."): only the greeting is cut
# (the greeting word, plus up to two address words when a mark follows them: "Hello team," / "Здравствуйте,"; with
# no mark only the greeting word itself goes, so "Hi I want to apply" keeps "I want to apply")
_GREETING_PREFIX_RE = re.compile(
    rf"^[ \t]*{_GREETING_WORDS}(?:(?:[ \t]+[^\s,!.]+){{0,2}}[,!.]+|[,!.]*)[ \t]*", re.I)


def ensure_greeting(text: str, lang: str) -> str:
    """The first line is exactly the greeting for ``lang`` ("Добрый день!" / "Hello,"), then a blank line.
    A greeting line at the start (with or without an address) is replaced; a greeting that starts a longer line
    is cut off and the rest of the line is kept; none present: the greeting is prepended. PURE."""
    greeting = GREETING_EN if lang == "en" else GREETING_RU
    body = (text or "").lstrip()
    if not body:
        return greeting
    first, _, rest = body.partition("\n")
    if _GREETING_LINE_RE.match(first):
        body = rest
    else:
        m = _GREETING_PREFIX_RE.match(first)
        if m:
            remainder = first[m.end():].strip()
            remainder = remainder[:1].upper() + remainder[1:]      # a sentence now starts the text
            body = (remainder + ("\n" + rest if rest else "")) if remainder else rest
    body = body.lstrip("\n")
    return f"{greeting}\n\n{body}" if body.strip() else greeting


# Lines the model writes itself although the signature block is appended by code ("GitHub: ...", "Сайт: ...",
# a resume placeholder): dropped, so the block is not doubled.
_OWN_SIGNATURE_RE = re.compile(
    r"(?im)^[ \t]*(?:github|git\s*hub|сайт|site|website|резюме|resume|cv|портфолио|portfolio)[ \t]*:.*$|"
    r"^[ \t]*\[(?:резюме|resume)[^\]\n]*\][ \t]*$"
)


def strip_own_signature(text: str, profile: Optional[Profile] = None) -> str:
    """Drop signature-like lines the model wrote itself (and a bare line holding the profile's GitHub link). PURE."""
    out = _OWN_SIGNATURE_RE.sub("", text or "")
    gh = ((profile or example_profile()).draft_signature.github or "").strip()
    if gh:
        # a short line that only points at the GitHub link ("Code on GitHub at github.com/x."), not a real sentence
        out = "\n".join(
            ln for ln in out.split("\n") if not (gh.lower() in ln.lower() and len(ln.strip()) <= gh.__len__() + 40)
        )
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# Characters of scripts the draft must never contain (the model sometimes slips a Korean / Chinese word in).
_FOREIGN_SCRIPT_RE = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\u0e00-\u0e7f\u0600-\u06ff]")


def has_foreign_script(text: str) -> bool:
    return bool(_FOREIGN_SCRIPT_RE.search(text or ""))


def strip_foreign_script(text: str) -> str:
    """Last resort when a regenerated draft still carries such characters: remove them. PURE."""
    return re.sub(r" {2,}", " ", _FOREIGN_SCRIPT_RE.sub("", text or ""))


def _strip_greeting(text: str) -> str:
    """The body without a greeting the model may have written at the start. PURE."""
    body = (text or "").lstrip()
    first, _, rest = body.partition("\n")
    if _GREETING_LINE_RE.match(first):
        body = rest
    else:
        m = _GREETING_PREFIX_RE.match(first)
        if m:
            remainder = first[m.end():].strip()
            remainder = remainder[:1].upper() + remainder[1:]
            body = (remainder + ("\n" + rest if rest else "")) if remainder else rest
    return body.lstrip("\n")


# The intro is written by code; if the model repeats it anyway, its first sentences are cut. Only at the very start.
_REPEATED_INTRO_RE = re.compile(
    r"^[ \t]*(?:откликаюсь|пишу\s+(?:по\s+поводу|в\s+связи)|хочу\s+откликнуться|я\s+(?:ai[- ]?)?инженер\b|"
    r"i['’]m\s+applying|i\s+am\s+applying|i['’]m\s+writing\s+to\s+apply|i['’]d\s+like\s+to\s+apply|"
    r"i['’]m\s+an?\s+(?:ai\s+)?engineer\b|i\s+am\s+an?\s+(?:ai\s+)?engineer\b)[^.!?\n]*[.!?]+[ \t]*",
    re.I,
)


def _strip_repeated_intro(text: str) -> str:
    out = text.lstrip()
    for _ in range(2):                                   # the application sentence and the "I'm an engineer" one
        m = _REPEATED_INTRO_RE.match(out)
        if not m:
            break
        out = out[m.end():].lstrip()
    return out[:1].upper() + out[1:] if out else out


# Final sentences that only state interest / readiness / a value or praise the stack: cut (the prompt forbids them,
# the model still writes them now and then). Only trailing sentences; a sentence about GitHub (not GitHub Actions)
# is cut anywhere, because the signature already carries the link and the model cannot know what the repo holds.
_EMPTY_TAIL_RE = re.compile(
    r"^\W*(?:готов(?:а)?\b|заинтересован(?:а)?\b|буду\s+рад|рад(?:а)?\b|с\s+радостью|хотел(?:а)?\s+бы\s+обсудить|"
    r"мне\s+(?:важн|близк|интересн)|я\s+ценю|"
    r"i['’]d\s+(?:be\s+(?:happy|glad|interested)|love)|i\s+would\s+(?:be\s+(?:happy|glad|interested)|love)|"
    r"i['’]m\s+(?:excited|interested|keen|eager|ready|happy)|looking\s+forward|happy\s+to\s+(?:discuss|chat|talk)|"
    r"feel\s+free|the\s+(?:tech\s+)?stack(?:\s+\w+){0,3}?\s+(?:lines\s+up|matches|maps|aligns|fits|overlaps)|"
    r"(?:ваш\s+)?стек\s+(?:вакансии\s+)?(?:совпадает|пересекается|близок|подходит)|"
    r"this\s+(?:role|position)\s+(?:is|looks|seems))",
    re.I,
)
# A sentence in which the candidate picks a seniority level / track for themselves or claims to lead people. The prompt
# forbids it, the model still does it now and then ("The principal track fits what I've been doing ...").
_LEVEL_CLAIM_RE = re.compile(
    r"\b(?:principal|staff|distinguished)\b|"
    r"\b(?:track|level)\b[^.!?]*\b(?:fits?|match(?:es)?|suits?)\b|\b(?:fits?|match(?:es)?|suits?)\b[^.!?]*\b(?:track|level)\b|"
    r"\btech\s*lead\b|\bteam\s*lead\b|\b(?:led|leading|lead)\s+(?:a\s+|the\s+|my\s+)?(?:team|developers|engineers)\b|"
    r"тимлид|тех\.?\s*лид|руководил[аи]?\s+команд|возглавлял[аи]?|(?:трек|уровень)\s+(?:principal|senior|lead|сеньор|лид)",
    re.I,
)
# Frequency / intensity phrases the candidate list does not support; only the phrase goes, the sentence stays.
_FREQUENCY_RE = re.compile(
    r",?\s*\b(?:ежедневно|постоянно|каждый\s+день|на\s+постоянной\s+основе|в\s+повседневной\s+работе|"
    r"daily|every\s+day|constantly|on\s+a\s+daily\s+basis|day[- ]to[- ]day)\b",
    re.I,
)
_GITHUB_MENTION_RE = re.compile(r"\bgit\s?hub\b(?!\s+actions)", re.I)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZА-ЯЁ«\"'(])")


def strip_empty_tail(text: str) -> str:
    """Cut closing sentences that say nothing ("Готова обсудить детали.", "I'd be interested in discussing ...") and
    sentences that mention GitHub. Never empties the text: if everything would go, the text is returned as it was. PURE."""
    if not (text or "").strip():
        return text
    paragraphs = [p for p in re.split(r"\n{2,}", text.strip())]
    sentences = []
    for pi, p in enumerate(paragraphs):
        for s in _SENTENCE_SPLIT_RE.split(p.strip()):
            if s.strip():
                sentences.append((pi, s.strip()))
    sentences = [(pi, s) for pi, s in sentences
                 if not _GITHUB_MENTION_RE.search(s) and not _LEVEL_CLAIM_RE.search(s)]
    for _ in range(3):                                      # trailing empty sentences, at most three in a row
        if len(sentences) > 1 and _EMPTY_TAIL_RE.match(sentences[-1][1]):
            sentences.pop()
        else:
            break
    if not sentences:
        return text
    out, current = [], None
    for pi, s in sentences:
        if pi != current:
            out.append(s)
            current = pi
        else:
            out[-1] += " " + s
    return "\n\n".join(out)


def normalize_company(text: str, company: Optional[str]) -> str:
    """The company name spelled exactly as on the card wherever the model wrote it in another case ("Acme" for
    "ACME"). Whole words only; nothing happens without a company. PURE."""
    name = (company or "").strip()
    if not text or len(name) < 2:
        return text
    return re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", name, text, flags=re.I)


def compose_draft(body: str, lang: str, intro: str) -> str:
    """greeting (line 1), the fixed intro (line 2), a blank line, then the model's text. The model's own greeting and
    any repeat of the intro are removed; no long dashes. PURE."""
    greeting = GREETING_EN if lang == "en" else GREETING_RU
    text = strip_long_dashes(body)
    for _ in range(4):                        # a model may repeat the greeting / the whole intro block more than once
        cleaned = _strip_repeated_intro(_strip_greeting(text))
        if cleaned == text:
            break
        text = cleaned
    if intro:                                 # the exact intro or a lone greeting line anywhere else in the body
        text = text.replace(intro, "")
    text = re.sub(rf"(?m)^[ \t]*{re.escape(greeting)}[ \t]*$", "", text)
    text = _FREQUENCY_RE.sub("", text)
    text = strip_empty_tail(re.sub(r"\n{3,}", "\n\n", text).strip())
    head = f"{greeting}\n{intro}".rstrip()
    return f"{head}\n\n{text}" if text.strip() else head


def polish_draft(text: str, raw_text: str, intro: str = "") -> str:
    """Greeting (+ the fixed intro when given) and no long dashes, in the language of the vacancy (the same rule the
    prompt uses). PURE."""
    lang = llm._detect_lang(raw_text or "")
    if intro:
        return compose_draft(text, lang, intro)
    return ensure_greeting(strip_long_dashes(text), lang)


def research(
    client: LLMClient,
    extracted: ExtractResult,
    raw_text: str,
    model: str = llm.CHEAP_MODEL,
) -> Dict[str, Any]:
    """Gather company/role context for T10. Returns a research dict.

    Phase 1.5 (#20): BEFORE asking the LLM, select the IN-BODY apply/company
    URL (``research_fetch.select_primary_url`` — the contact-as-link, else the
    first non-telegram URL in the post; NOT the t.me permalink) and attempt a
    direct-URL fetch of it (and, best-effort, one company page) via
    ``research_fetch``. When usable page text is retrieved it is fed to the LLM
    as clearly-labeled real
    page text and ``research_source`` is "web"; otherwise we fall back to the
    desk-only research that has always run here (``research_source`` =
    "desk_fallback"), giving exactly today's behavior.

    The fetch NEVER raises (it's wrapped defensively), and the whole fetch/
    decision is guarded so research NEVER raises out of this function — any
    unexpected error degrades to desk research. ``research_source`` and
    ``fetched_urls`` are set AUTHORITATIVELY here (not trusting the model). The
    boundary, signature, and the dict the draft consumes are unchanged.
    """
    fetched: Dict[str, Any] = {"pages": [], "urls": []}
    try:
        # Phase 1.5 (#20): the Telegram ``source_link`` is ALWAYS the post
        # permalink (a t.me embed shell, ~181 chars -> discarded), never the
        # real apply/company target. Select the IN-BODY apply/company URL (the
        # contact-as-link, else the first non-telegram URL in the post) and
        # fetch THAT. None -> empty pages -> desk_fallback (today's behavior).
        primary_url = research_fetch.select_primary_url(extracted, raw_text)
        fetched = research_fetch.fetch_research_context(
            primary_url, extracted.company
        )
    except Exception:
        fetched = {"pages": [], "urls": []}

    pages = fetched.get("pages") or []
    if pages:
        research_source = "web"
        fetched_context = fetched
        fetched_urls = list(fetched.get("urls") or [])
    else:
        research_source = "desk_fallback"
        fetched_context = None
        fetched_urls = []

    try:
        data = llm.llm_research(
            client, extracted, raw_text, model=model, fetched_context=fetched_context
        )
    except Exception:
        # research must NEVER raise out of here: an LLM-layer failure (timeout,
        # rate limit, un-parseable output) would propagate through _do_research
        # into advance() and stall the item in APPROVED. Degrade to an empty
        # research blob — the draft falls back to the post text.
        data = {"summary": "", "talking_points": [], "questions": [], "sourced_facts": []}
    if not isinstance(data, dict):
        data = {"summary": "", "talking_points": [], "questions": [], "sourced_facts": []}
    # Authoritative provenance — never trust the model for these.
    data["research_source"] = research_source
    data["fetched_urls"] = fetched_urls
    # Honesty: with no fetched page there is NO grounding, so there can be no
    # page-sourced facts — clear anything the model may have invented so
    # fabricated "facts" never reach the draft as if they were real.
    if research_source == "desk_fallback":
        data["sourced_facts"] = []
    return data


def draft(
    client: LLMClient,
    extracted: ExtractResult,
    raw_text: str,
    research_data: Optional[Dict[str, Any]] = None,
    model: str = llm.CHEAP_MODEL,
    profile: Optional[Profile] = None,
) -> str:
    """Generate an application draft via the LLM (T11). Returns message text.

    The deterministic GitHub + резюме signature is appended after generation so
    the literal links are ALWAYS present regardless of what the model emits. The
    ``profile`` (when given) drives both the prompt and the signature; when None
    the GENERIC example profile is used.
    """
    body = llm.llm_draft(
        client, extracted, raw_text, research_data, model=model, profile=profile
    )
    if has_foreign_script(body):
        # the model slipped Korean / Chinese characters into a word: one more try, then cut what is left
        body = llm.llm_draft(
            client, extracted, raw_text, research_data, model=model, profile=profile
        )
        if has_foreign_script(body):
            body = strip_foreign_script(body)
    intro = llm.draft_intro(extracted, raw_text, profile)
    body = normalize_company(body, extracted.company)
    body = strip_own_signature(polish_draft(body, raw_text, intro), profile)
    return append_draft_signature(body, profile)


def merge_aux(extracted_json: Optional[str], key: str, value: Any) -> str:
    """Merge an auxiliary key (research/draft) into an extracted_json blob.

    PURE. Keeps the §4 schema fields intact and stores agent output alongside.
    """
    try:
        data = json.loads(extracted_json) if extracted_json else {}
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[key] = value
    return json.dumps(data, ensure_ascii=False, sort_keys=True)
