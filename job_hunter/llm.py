"""LLM calls via the Anthropic SDK, with per-step model routing.

I/O module. Pure prompt-building and response-parsing helpers are split out so
they can be unit-tested without a live client. The Anthropic client is created
lazily and is injectable for tests.

Model routing (the model id is chosen PER CALL, not per client):
  - T1  smart extract (raw_text -> Extract schema)  -> cheap_model (Haiku)
  - relevance score (rubric judgement + Обоснование) -> judge_model (Sonnet)
  - T10 research / enrich                            -> cheap_model (Haiku)
  - T11 draft generation                             -> cheap_model (Haiku)

``complete(system, user, model=...)`` takes an explicit ``model`` so the caller
controls routing. The client carries a ``model`` default used when none is
passed (back-compat). Tests assert the requested model id per call.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Protocol

from .profile import Profile, example_profile
from .schema_extract import ExtractResult, from_dict

# Default model ids. Overridable via config; passed explicitly per call.
DEFAULT_MODEL = "claude-haiku-4-5"
CHEAP_MODEL = "claude-haiku-4-5"      # bulk/cheap steps (extraction, research, draft)
JUDGE_MODEL = "claude-sonnet-4-6"     # judgment/scoring step


# --- Per-step max_tokens (response headroom; COST-CAP only, never alters a
# normal response) ----------------------------------------------------------
#
# These cap the model's OUTPUT length. For a well-formed JSON response the model
# emits the closing ``}`` and STOPS well before the cap, so raising a limit does
# NOT change the bytes of a normal response — it only adds headroom so a long-
# but-valid response is not truncated mid-JSON. Truncation is the confirmed #20
# failure mode: a cut-off response has no closing ``}`` and cannot be parsed,
# which then SILENTLY degrades the caller (extract->heuristic, score->fallback
# score, research->empty). The limits below were chosen per step to remove that
# risk for realistic worst-case outputs.
#
# extract: long stack + benefits + reasons arrays plus company/contact can
#   approach the old 800; Cyrillic labels tokenize heavily. 1200 gives ~50%
#   headroom so a rich post no longer truncates into the heuristic fallback
#   (which also loses the contact-as-link URL #20 depends on).
EXTRACT_MAX_TOKENS = 1200
# score: the «Обоснование» is a verdict line + 2-4 FULL-sentence Cyrillic
#   bullets; Cyrillic is token-heavy (~1.7 chars/token) so a verbose-but-valid
#   rationale easily exceeded the old 400 -> truncation -> silent score
#   degradation. 800 comfortably fits the verdict + four full sentences.
SCORE_MAX_TOKENS = 800
# research: with FETCHED PAGE TEXT grounding, summary + talking_points +
#   questions + sourced_facts is large; the old 900 truncated mid-output (the
#   confirmed #20 break, cut at ``"questions":``). 2000 fits the bounded
#   contract (talking_points capped at <=6 in the prompt).
RESEARCH_MAX_TOKENS = 2000
# draft: free-text отклик, ~90-150 words; 500 is already ample. Unchanged.
DRAFT_MAX_TOKENS = 500


# --- Prompt-caching policy (COST-ONLY: never changes model output) ----------
#
# Anthropic prompt caching only ENGAGES when the cached prefix is at least the
# model's minimum cacheable length. That minimum differs by model family:
#   - Sonnet (claude-sonnet-4-*): ~1024 tokens
#   - Haiku  (claude-haiku-4-*):  ~2048 tokens
# Below the minimum, ``cache_control`` is silently ignored by the API, so adding
# it there only adds request noise. We therefore measure each CONSTANT system
# prompt and add ``cache_control`` ONLY when it exceeds its model's minimum.
#
# Token length is estimated offline (no network): plain ASCII tokenizes at
# roughly chars/4, while Cyrillic / emoji tokenize less efficiently. We use a
# conservative blended estimate so a borderline-but-qualifying prefix is not
# wrongly skipped.
_SONNET_CACHE_MIN_TOKENS = 1024
_HAIKU_CACHE_MIN_TOKENS = 2048


def cache_min_tokens_for_model(model: Optional[str]) -> int:
    """Minimum cacheable prefix length (in tokens) for ``model``. PURE.

    Haiku requires ~2048 tokens; Sonnet (and anything else, conservatively the
    smaller floor) requires ~1024.
    """
    m = (model or "").lower()
    if "haiku" in m:
        return _HAIKU_CACHE_MIN_TOKENS
    return _SONNET_CACHE_MIN_TOKENS


def estimate_tokens(text: str) -> int:
    """Offline token-count estimate for a prompt string. PURE, no network.

    ASCII ~ chars/4 tokens; non-ASCII (Cyrillic / emoji) tokenizes worse, ~1.7
    chars/token. This is only used to decide whether a prefix clears the cache
    minimum; it never affects model output.
    """
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii = len(text) - ascii_chars
    return round(ascii_chars / 4 + non_ascii / 1.7)


def should_cache_system(system: str, model: Optional[str]) -> bool:
    """True when the CONSTANT system prefix is long enough to cache on ``model``.

    PURE. Compares the offline token estimate against the per-model minimum.
    """
    return estimate_tokens(system) >= cache_min_tokens_for_model(model)


def build_system_param(system: str, cache: bool):
    """Build the ``system`` request field. PURE.

    When ``cache`` is False, return the plain string (back-compat, no caching).
    When ``cache`` is True, return a single structured text block carrying
    ``cache_control`` so the Anthropic API caches this constant prefix. The TEXT
    is byte-identical to ``system`` either way — only the transport shape and
    billing change, never the model output.
    """
    if not cache:
        return system
    return [
        {
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }
    ]


class LLMClient(Protocol):
    """Minimal protocol our code depends on (subset of Anthropic SDK).

    ``model`` is optional: when None the client uses its configured default.
    Callers that need routing pass an explicit model id per call.

    ``cache_system`` (cost-only) requests that the CONSTANT system prefix be
    transmitted with ``cache_control`` so the API can cache it. It never changes
    the model output; implementations that don't cache may ignore it.
    """

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        model: Optional[str] = None,
        cache_system: bool = False,
    ) -> str:
        ...


class AnthropicClient:
    """Thin wrapper over the Anthropic Messages API with per-call model routing."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        # Imported lazily so tests / pure paths don't require the package config.
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        model: Optional[str] = None,
        cache_system: bool = False,
    ) -> str:
        # cache_system marks the CONSTANT system prefix with cache_control. The
        # per-item VARIABLE content (vacancy text) stays in the USER message, so
        # the cached prefix is byte-identical across every item in a run. This is
        # cost-only: the system TEXT and the request's model/max_tokens/messages
        # are unchanged, so the response is identical.
        resp = self._client.messages.create(
            model=model or self.model,
            max_tokens=max_tokens,
            system=build_system_param(system, cache_system),
            messages=[{"role": "user", "content": user}],
        )
        parts: List[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "".join(parts)


# --- Pure helpers: prompt building + response parsing -----------------------

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.S)

# Leading code-fence marker: an optional ```/```json opener (possibly on its own
# line) at the START of the response. Stripped as a PREPROCESS step so a fence is
# removed even when the closing ``` is missing/truncated or sits on its own line.
_LEADING_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?[ \t]*\r?\n?", re.S)
# Trailing closing fence (when present). Optional — a truncated response may lack
# it, in which case we simply leave the body unfenced.
_TRAILING_FENCE_RE = re.compile(r"\r?\n?[ \t]*```\s*$", re.S)


def _parse_json_object(text: str) -> Dict[str, Any]:
    """Extract the first JSON object from a model response. PURE.

    Fence handling is a PREPROCESS step (defense-in-depth): we strip a LEADING
    ```/```json marker (and any TRAILING ```), then run the same object
    extraction on the cleaned text. This salvages fenced responses regardless of
    fence placement and even when the closing fence was truncated away — yet an
    already-unfenced response is byte-for-byte unchanged by the strip (the
    leading/trailing regexes only match when fence markers are actually present),
    so the common case parses identically to before.

    A genuinely TRUNCATED object (no closing ``}``) still cannot be salvaged and
    raises ValueError cleanly — that case is fixed upstream by adequate
    ``max_tokens`` headroom, not by inventing the missing data here.
    """
    text = text.strip()
    # PREPROCESS: peel a leading fence marker, then a trailing one if present.
    # This runs BEFORE object extraction so it works for partial / oddly-placed
    # fences (missing close, marker on its own line). When no fence is present
    # both subs are no-ops, preserving the unfenced behavior exactly.
    cleaned = _LEADING_FENCE_RE.sub("", text)
    if cleaned != text:
        # Only strip a trailing close fence when we actually opened one above,
        # so a stray ``` inside unfenced prose is never touched.
        cleaned = _TRAILING_FENCE_RE.sub("", cleaned)
    cleaned = cleaned.strip()

    m = _JSON_OBJ_RE.search(cleaned)
    if not m:
        raise ValueError("no JSON object found in LLM response")
    return json.loads(m.group(0))


EXTRACT_SYSTEM = (
    "You parse ONE job posting into strict JSON. Output ONLY a JSON object, no "
    "prose. Judge by stack and responsibilities, not the job title. Use null "
    "for unknown values. contact_type must be one of dm|form|link or null.\n"
    "COMPANY: if the post has an explicit label line like 'Компания: X', "
    "'Company: X', 'Работодатель: X', or 'Employer: X', set company to exactly "
    "that name (e.g. 'Компания: Нетбелл' -> company='Нетбелл'). Read the WHOLE "
    "post for it, including near the top. Use null only when no company is "
    "named.\n"
    "SALARY: extract ONLY monetary pay. NEVER read a date or a deadline as "
    "salary. Phrases like 'до 31 мая', 'until May 31', 'deadline', 'дедлайн', "
    "'по 30 июня', application windows, or any day/month/year are NOT salary -> "
    "leave salary_min/salary_max null unless a real money amount is stated.\n"
    "CONTACT: this is the RECRUITER / application contact found INSIDE the "
    "posting body (an @handle, an email, or an application URL written in the "
    "text). SCAN THE WHOLE POST, including the very BOTTOM / last lines — the "
    "contact is often at the end (e.g. 'Контакты: info@example.com'). It is NOT "
    "the source channel the post was scraped from; never use the source channel "
    "as the contact. If the only handle present is the channel itself, leave "
    "contact null. contact_type: use 'dm' for an @handle/telegram, 'link' for a "
    "URL, and for a bare email leave contact_type null (no enum slot) while "
    "still putting the email address in contact.\n"
    "REMOTE / LOCATION / SENIORITY: READ THE FULL POST — the top lines, any "
    "«Что мы предлагаем» / benefits block, the body, AND the HASHTAGS (often a "
    "trailing row like '#УдаленкаРФ #middle #Москва'). Resolve remote from ANY "
    "of these, not just the first lines.\n"
    "- remote = true for fully-remote signals ANYWHERE: 'Полная удалёнка', "
    "'удалёнка', 'удаленка', 'удаленно', 'remote', 'fully remote', or hashtags "
    "#УдаленкаРФ / #Удаленка / #Удалёнка / #Remote / #удаленно (be tolerant of "
    "case and the ё/е variation).\n"
    "- remote = false for office-only signals with no remote: 'офис', 'в офисе', "
    "'office', 'on-site'.\n"
    "- HYBRID ('гибрид' / 'hybrid', or both remote and office offered): set "
    "remote = true (hybrid includes remote work) AND append '(гибрид)' to the "
    "location field to record the hybrid nuance.\n"
    "- remote = null only when NOTHING indicates a work format.\n"
    "- LOCATION from hashtags: #Москва/#Moscow -> 'Москва'; #СПб/#Питер/#SPb -> "
    "'Санкт-Петербург'; #Регионы -> 'Регионы'. Set the location field.\n"
    "- SENIORITY from hashtags: #junior/#джун -> junior; #middle/#миддл -> "
    "middle; #senior/#синьор -> senior; #lead/#тимлид -> lead.\n"
    "BENEFITS (display only): extract a SHORT list of perks / conditions the "
    "company offers, read from a «Что мы предлагаем» / «Мы предлагаем» / «We "
    "offer» / benefits section, the HASHTAGS, and the body. Recognize items like "
    "ДМС / медстраховка (health insurance), удалёнка as a perk, обучение / курсы "
    "(learning budget), помощь с релокацией / релокация (relocation), визовый "
    "спонсор (visa sponsorship), техника / оборудование (equipment), бонусы / "
    "премии (bonus), оплачиваемый отпуск (paid vacation), спорт / фитнес, "
    "занятия английским (language classes), гибкий график (flexible hours). "
    "Return concise canonical labels in the SAME language as the posting. Use an "
    "empty list [] when none are stated. These are informational ONLY and do NOT "
    "affect any score."
)


def build_extract_prompt(raw_text: str, source_channel: str, source_link: Optional[str]) -> str:
    schema = {
        "title": "string",
        "company": "string|null",
        "stack": ["lowercase tokens, e.g. python, llm, rag, aiogram, fastapi, docker"],
        "seniority": "junior|middle|middle+|senior|lead|null",
        "salary_min": "number|null (money only, never a date)",
        "salary_max": "number|null (money only, never a date)",
        "currency": "ISO code like RUB/USD/EUR or null",
        "remote": "true|false|null",
        "relocation": "true|false|null",
        "location": "string|null",
        "contact_type": "dm|form|link|null",
        "contact": "string|null (recruiter contact from the post BODY, not the channel)",
        "benefits": ["short perk/condition labels in the post's language, e.g. ДМС, обучение, помощь с релокацией; [] if none"],
    }
    return (
        f"source_channel (DO NOT use as contact): {source_channel}\n"
        f"source_link: {source_link or ''}\n\n"
        f"Return JSON exactly matching these keys:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"JOB POST:\n{raw_text}"
    )


def parse_extract_response(
    text: str, source_channel: str, source_link: Optional[str]
) -> ExtractResult:
    """Turn a raw LLM extract response into an ExtractResult. PURE.

    Guards: the source channel must never leak into ``contact`` (that field is
    the recruiter contact from the post body, the channel belongs in
    ``source_channel``/``source_link``).
    """
    data = _parse_json_object(text)
    data.setdefault("source_channel", source_channel)
    data.setdefault("source_link", source_link)
    data.setdefault("reasons", [])
    data.setdefault("benefits", [])  # display-only; default [] for back-compat
    data["relevance_score"] = None  # populated later by score()

    if _contact_is_channel(data.get("contact"), source_channel, source_link):
        data["contact"] = None
        data["contact_type"] = None

    return from_dict(data)


def _contact_is_channel(
    contact: Optional[str], source_channel: Optional[str], source_link: Optional[str]
) -> bool:
    """True when the extracted contact is actually the source channel (a leak).

    PURE. Compares the contact against the channel username and the source
    permalink so a model mistakenly echoing the channel does not poison the
    contact field.
    """
    if not contact:
        return False
    c = contact.strip().lower().lstrip("@").rstrip("/")
    if not c:
        return False
    if source_channel:
        chan = source_channel.strip().lower().lstrip("@").rstrip("/")
        chan = re.sub(r"^https?://t\.me/(s/)?", "", chan)
        if chan and (c == chan or c == f"https://t.me/{chan}"):
            return True
    if source_link:
        link = source_link.strip().lower().rstrip("/")
        if c == link:
            return True
    return False


# --- Relevance SCORE (Sonnet rubric judgement, replaces the old tiebreak) ---

def _render_bullets(items: List[str]) -> str:
    """Render a list of profile lines as '  - ...' bullets. PURE."""
    return "\n".join(f"  - {line}" for line in items) if items else "  - (none)"


def render_profile_block(profile: Profile) -> str:
    """Render the candidate-SPECIFIC CANDIDATE PROFILE block from the loaded
    profile DATA. PURE.

    The rubric / judge logic stays in code (see ``build_score_system``); this
    function only formats the personal DATA (role, grade, hands-on, stack,
    languages, salary floor) that was loaded from YAML. No personal specifics
    are hardcoded here — they all come from ``profile``.
    """
    floor = int(round(profile.salary_floor_eur))
    return (
        "CANDIDATE PROFILE (loaded from the local profile YAML):\n"
        f"- Role: {profile.role}\n"
        f"- Grade: {profile.target_grade} A junior-titled role is acceptable "
        f"ONLY if the salary is >= EUR {floor}/month. {profile.experience_note}\n"
        f"- Hands-on (may be claimed as real experience):\n"
        f"{_render_bullets(profile.hands_on)}\n"
        f"- In development (NOT hands-on yet — being learned, do not count as "
        f"done experience):\n"
        f"{_render_bullets(profile.in_development)}\n"
        f"- Stack:\n{_render_bullets(profile.stack)}\n"
        f"- Languages: {profile.languages}\n"
        f"- Location priority (highest -> lowest):\n"
        f"{_render_bullets(profile.location_priority)}"
    )


# JUDGE + RUBRIC scaffold (no personal DATA — the profile block is injected).
# {profile_block} is replaced with the rendered CANDIDATE PROFILE.
_SCORE_SCAFFOLD = (
    "You are a hiring-fit JUDGE for a specific candidate. Read ONE job posting "
    "and rate how well the ROLE itself (responsibilities + stack, NOT the title) "
    "fits THIS candidate. Output a relevance_score 0-100 and a short "
    "human-readable rationale.\n\n"
    "{profile_block}\n\n"
    "RUBRIC:\n"
    "- STRONG FIT (high score): roles where the candidate OWNS the AI / model "
    "behavior — Prompt / AI / LLM Engineer, AI Product Engineer, applied-LLM; "
    "where RAG / routing / prompts are the CORE of the job; grade matches the "
    "target grade above.\n"
    "- LOCATION PRIORITY: score location against the candidate's LOCATION "
    "PRIORITY ladder in the profile above (relocation / remote / hybrid / "
    "office, in that preference order). Roles abroad with relocation or visa "
    "sponsorship rank high when relocation is a stated goal.\n"
    "- FLAG / DOWN-WEIGHT (do NOT reject, just lower the score): salary "
    "unstated; an English-required role demanding fluent English 'right now' "
    "when the profile's language note flags that as a current gap; a senior "
    "role requiring proven tenure / a formal employment record.\n"
    "- LOW / REJECT-WORTHY (low score): backend developer with LLM 'on the "
    "side' (underfit); pure PM; marketing / lead-gen; DS / ML model training; "
    "Python livecoding from scratch / a hardcore algorithmic interview "
    "(near-certain failure); a HARD requirement for a technical degree "
    "(mandatory wording) when the profile says the candidate has none -> flag + "
    "low score (this applies ONLY when the post WORDS the degree as HARD — see "
    "REQUIREMENT SEVERITY below; a degree merely 'preferred / будет плюсом' is "
    "SOFT and gets only a light deduction, NOT a flag); lead / "
    "management -> not a fit.\n\n"
    "REQUIREMENT SEVERITY (classify EACH requirement by the POST'S OWN WORDING):\n"
    "- HARD = the post words it as mandatory: 'обязательно' / 'required' / "
    "'must' / 'необходимо' / 'строго' / 'mandatory' (and equivalents).\n"
    "- SOFT = the post words it as optional / desirable: 'будет плюсом' / "
    "'желательно' / 'preferred' / 'nice to have' / 'as a plus' / 'приветствуется' "
    "(and equivalents).\n"
    "- A SOFT requirement the candidate misses -> ONLY a SMALL score reduction. "
    "It MUST NOT be a ⚠️ hard flag and MUST NOT be phrased as a 'risk of "
    "rejection' / blocker; note it in SOFTER, neutral language (a mild caveat or "
    "a ✅-with-nuance), never as a rejection risk.\n"
    "- A HARD requirement the candidate misses -> keep full weight: flag it, ⚠️ "
    "is allowed, and it may lower the score as today.\n"
    "- AMBIGUOUS wording (the post does NOT clearly say mandatory or optional) -> "
    "DEFAULT TO SOFT. Do not invent hard severity.\n\n"
    "Do NOT consider salary thresholds — a separate deterministic guard handles "
    "pay. Judge ROLE FIT only.\n\n"
    "OUTPUT FORMAT for «Обоснование» (this constrains the PRESENTATION, not the "
    "judgment above). Write it in the SAME language as the posting and structure "
    "it as a scannable verdict + bullets:\n"
    "- LINE 1: a VERDICT line that INCLUDES the score number (e.g. «Сильный фит "
    "по роли — 82/100» or 'Strong role fit — 82/100').\n"
    "- THEN 2-4 BULLETS, each starting with ✅ (a fit / plus) or ⚠️ (a risk / "
    "caveat). Cover the CORE FIT (role / stack / level: does the candidate own "
    "the AI / model behavior, does the stack overlap, is the grade right) AND "
    "the MAIN caveat or risk.\n"
    "- ⚠️ is RESERVED for genuine HARD misses / real risks (see REQUIREMENT "
    "SEVERITY). A SOFT / 'будет плюсом' / 'preferred' gap the candidate lacks is "
    "NOT a ⚠️ rejection risk — mention it in softer, non-⚠️ language (a neutral "
    "caveat or a ✅-with-nuance).\n"
    "- CRITICAL: each bullet MUST be a FULL, SHORT SENTENCE — substantive and "
    "self-contained — NOT a choppy fragment and NOT a single word. "
    "GOOD (full sentence, do this): «✅ Роль — про внедрение LLM (RAG, "
    "prompt-eng), что точно совпадает с её хендз-он опытом.» "
    "BAD (terse fragment, FORBIDDEN): «✅ RAG».\n"
    "- Keep it scannable (the verdict line plus the bullets), but every bullet a "
    "complete sentence. Do NOT write a single flowing paragraph / wall of text, "
    "and do NOT write single-word fragments.\n\n"
    'Output ONLY JSON: {"relevance_score": <int 0-100>, "Обоснование": '
    '"<verdict line WITH the score number, then 2-4 ✅/⚠️ FULL-SENTENCE bullets, '
    'all in the SAME language as the posting>"}.'
)


def build_score_system(profile: Profile) -> str:
    """Render the full SCORE system prompt from the loaded profile. PURE.

    The JUDGE instructions + RUBRIC structure + OUTPUT FORMAT live in
    ``_SCORE_SCAFFOLD`` (code); the candidate-SPECIFIC DATA is injected via
    ``render_profile_block(profile)``. No personal specifics are hardcoded.
    """
    return _SCORE_SCAFFOLD.replace("{profile_block}", render_profile_block(profile))


# Module-level constant rendered from the GENERIC example profile so the module
# carries NO personal data. Live runs build the prompt from the loaded (local)
# profile via ``build_score_system`` wired through the pipeline.
SCORE_SYSTEM = build_score_system(example_profile())


def build_score_prompt(extracted: ExtractResult, raw_text: str) -> str:
    payload = {
        "title": extracted.title,
        "company": extracted.company,
        "stack": extracted.stack,
        "seniority": extracted.seniority,
        "remote": extracted.remote,
        "relocation": extracted.relocation,
        "location": extracted.location,
    }
    return (
        "Rate this job's role-fit and explain why.\n"
        "Structured fields:\n" + json.dumps(payload, ensure_ascii=False)
        + "\n\nOriginal posting:\n" + raw_text
    )


def parse_score_response(text: str) -> Dict[str, Any]:
    """Parse the Sonnet score response into {'score': int 0-100, 'reasoning': str}.

    PURE and STRICT: clamps the score into [0, 100], coerces to int, and accepts
    the rationale from the Russian key 'Обоснование' (preferred) or fall-backs
    ('reasoning'/'reason'). Raises ValueError if no score-like number is present.
    """
    data = _parse_json_object(text)

    raw_score = data.get("relevance_score", data.get("score"))
    if raw_score is None:
        raise ValueError("score response missing relevance_score")
    try:
        score = int(round(float(raw_score)))
    except (TypeError, ValueError):
        raise ValueError(f"score not numeric: {raw_score!r}")
    score = max(0, min(100, score))

    reasoning = (
        data.get("Обоснование")
        or data.get("reasoning")
        or data.get("reason")
        or ""
    )
    return {"score": score, "reasoning": str(reasoning).strip()}


RESEARCH_SYSTEM = (
    "You research a company/role for a job applicant. Be concise and factual. "
    "If you are unsure, say so. Output ONLY a RAW JSON object — NO markdown, NO "
    "code fence (do NOT wrap it in ```), nothing before or after the JSON. "
    "Output ONLY JSON: "
    '{"summary": "...", "talking_points": ["..."], "questions": ["..."], '
    '"sourced_facts": ["..."]}.\n'
    "Keep `talking_points` SHORT: AT MOST 6 items (prefer 3-5). Keep each item a "
    "single concise sentence so the whole JSON stays compact.\n"
    "HONESTY (CRITICAL — never fabricate company facts):\n"
    "- The prompt may contain a section labeled 'FETCHED PAGE TEXT (real, from "
    "<url>)'. ONLY statements you can ground in that fetched page text are "
    "confirmed company facts — put EACH such fact (verbatim or faithfully "
    "paraphrased) into the dedicated `sourced_facts` list.\n"
    "- Everything NOT grounded in the fetched page text is your own inference. "
    "It may appear in `summary` / `talking_points` / `questions`, but you MUST "
    "NOT state it as a confirmed company fact, and it MUST NOT go in "
    "`sourced_facts`.\n"
    "- The 'ORIGINAL POST' section is candidate-relevant context but is NOT "
    "verified company information — do not treat it as a sourced fact.\n"
    "- If there is NO fetched page text, or it contains no real company "
    "information, set `sourced_facts: []` and say so explicitly in the summary "
    "(include the note «информация о компании со страницы недоступна») rather "
    "than inventing anything.\n"
    "- NEVER invent company facts, funding, headcount, products, or clients."
)


def build_research_prompt(
    extracted: ExtractResult,
    raw_text: str,
    fetched_context: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the research user prompt. PURE.

    When ``fetched_context`` carries usable page text, lay out two CLEARLY
    LABELED sections: the real FETCHED PAGE TEXT (the only confirmed-fact
    source) and the candidate-relevant ORIGINAL POST. When there is no fetched
    context, the output is BYTE-IDENTICAL to the desk-only prompt (so the
    fallback path behaves exactly like before).
    """
    payload = {
        "title": extracted.title,
        "company": extracted.company,
        "stack": extracted.stack,
        "location": extracted.location,
    }
    desk = (
        "Job context:\n" + json.dumps(payload, ensure_ascii=False)
        + "\n\nOriginal post:\n" + raw_text
    )

    pages = (fetched_context or {}).get("pages") if fetched_context else None
    if not pages:
        return desk

    blocks: List[str] = []
    for page in pages:
        url = page.get("url", "")
        ptext = page.get("text", "")
        if not ptext:
            continue
        blocks.append(
            f"FETCHED PAGE TEXT (real, from {url}) — only facts here are "
            f"confirmed company facts:\n{ptext}"
        )
    if not blocks:
        return desk

    return (
        "Job context:\n" + json.dumps(payload, ensure_ascii=False)
        + "\n\n" + "\n\n".join(blocks)
        + "\n\nORIGINAL POST (candidate-relevant, not necessarily verified):\n"
        + raw_text
    )


# Hard cap on talking_points so the research blob stays bounded even if a model
# ignores the prompt's "<=6" instruction. Defense in depth; matches the prompt.
RESEARCH_MAX_TALKING_POINTS = 6


def parse_research_response(text: str) -> Dict[str, Any]:
    data = _parse_json_object(text)
    return {
        "summary": str(data.get("summary", "")),
        "talking_points": list(data.get("talking_points", []) or [])[
            :RESEARCH_MAX_TALKING_POINTS
        ],
        "questions": list(data.get("questions", []) or []),
        "sourced_facts": list(data.get("sourced_facts", []) or []),
    }


_GENDER_RULES = {
    "female": (
        "CANDIDATE GENDER (CRITICAL): the candidate is a WOMAN. When writing in "
        "Russian, EVERY first-person self-reference about the candidate MUST use "
        "FEMININE grammatical gender. Use feminine verb/adjective forms: "
        "«заинтересована» (NOT «заинтересован»), «работала» (NOT «работал»), "
        "«реализовывала» / «реализовала» (NOT «реализовывал»), «готова» (NOT "
        "«готов»), «рада» (NOT «рад»), «сделала» (NOT «сделал»), «занималась», "
        "«писала», «спроектировала». Never use masculine self-reference for the "
        "candidate. (English drafts are unaffected — English has no grammatical "
        "gender here — but in Russian always use feminine forms for the candidate.)"
    ),
    "male": (
        "CANDIDATE GENDER (CRITICAL): the candidate is a MAN. When writing in "
        "Russian, EVERY first-person self-reference about the candidate MUST use "
        "MASCULINE grammatical gender («заинтересован», «работал», «готов», "
        "«рад», «сделал», «занимался»). (English drafts are unaffected.)"
    ),
    "unspecified": (
        "CANDIDATE GENDER (CRITICAL): the candidate's grammatical gender is "
        "UNSPECIFIED. When writing in Russian, AVOID gendered first-person "
        "self-reference: prefer gender-neutral phrasings (nouns / present-tense / "
        "infinitive constructions, e.g. «мой опыт», «занимаюсь», «готов(а)» only "
        "if unavoidable) rather than committing to masculine or feminine past-"
        "tense forms. (English drafts are unaffected.)"
    ),
}


def _gender_rule(gender: str) -> str:
    return _GENDER_RULES.get((gender or "").lower(), _GENDER_RULES["unspecified"])


# DRAFT scaffold (no personal DATA). {gender_rule}, {hands_on}, {in_development}
# are injected from the loaded profile.
_DRAFT_SCAFFOLD = (
    "You write a SHORT first-contact application message (отклик) for an "
    "applied-LLM engineer. Keep it concise: aim for ~90-150 words, shorter is "
    "fine. No markdown headers. Match the post's language (Russian or English). "
    "Reference concrete stack overlap. Output ONLY the message text.\n\n"
    "AUDIENCE (CRITICAL): this отклик is read FIRST by a NON-TECHNICAL "
    "RECRUITER, not a tech lead. Write so a non-technical person understands "
    "it. LEAD with plain-language fit — what the candidate actually does and why "
    "they fit the role, in everyday words a recruiter can follow. Keep deep "
    "jargon LIGHT: you may name a few core tools, but do NOT bury the message in "
    "technical detail the recruiter can't parse.\n\n"
    "{gender_rule}\n\n"
    "HONESTY (CRITICAL — represent the candidate ACCURATELY, never inflate): "
    "claim ONLY the candidate's HANDS-ON skills, and frame IN-DEVELOPMENT items "
    "honestly as something the candidate is learning / building toward, NEVER as "
    "completed experience.\n"
    "- HANDS-ON (may claim as real experience):\n{hands_on}\n"
    "- IN DEVELOPMENT, NOT hands-on (do NOT present as done experience):\n"
    "{in_development}\n"
    "- Python note: when the profile says the candidate reads / reviews / "
    "architects Python rather than writing it from scratch, never claim they "
    "write production Python from scratch.\n"
    "- Never invent experience the candidate does not have. If the job requires "
    "something they are only learning, frame it honestly ('интересно / хочу "
    "развиваться в' / 'interested in / building toward'), not as completed "
    "work.\n\n"
    "TERM LANGUAGE (when writing in Russian — natural RF-developer style, NOT a "
    "Latin-script salad): write common tech terms in CYRILLIC transliteration "
    "the way Russian developers actually write them — промпты, воркфлоу, эвалы, "
    "фреймворки, кеширование, роутинг, пайплайны, эмбеддинги. Do NOT scatter "
    "Latin words like 'workflow', 'quality', 'evaluation', 'active human eval', "
    "'area' through Russian text. A SHORT allowlist of terms stays Latin "
    "because that IS the Russian-dev norm: RAG, LLM, Docker, API, Python, "
    "Qdrant, FastAPI — use these as-is, but do not overload the message with "
    "Latin. "
    "GOOD (do this): «строю RAG-пайплайны, пишу промпты и гоняю эвалы». "
    "BAD (do NOT do this): «building RAG workflow with active human eval in "
    "this area».\n\n"
    "QUESTIONS (default to NONE; recruiter-readable only): READ THE FULL "
    "vacancy text below. Ask AT MOST ONE simple, plain-language question, and "
    "PREFER asking none. Only ask about information that is GENUINELY MISSING "
    "from the post — if the post already specifies the stack, frameworks "
    "(LangChain / LlamaIndex), the vector DB, the salary, or the work format, do "
    "NOT ask about those, and if everything relevant is covered, DROP the "
    "questions entirely (write NO filler questions). "
    "FORBIDDEN regardless of whether the info is missing: deep-technical "
    "questions a non-technical recruiter cannot parse. For example, a question "
    "like «автоматические метрики или active human eval?» MUST NOT appear — a "
    "recruiter cannot answer that. Any 'human eval' / metrics-internals question "
    "is a FORBIDDEN example. If your only question would be technical, ask "
    "nothing.\n\n"
    "TONE / DE-SLOP (must NOT read as AI-generated): sound like a competent "
    "person writing a short, specific, natural message — concise and human. "
    "Cut generic enthusiasm, filler, and performative phrasing. Do NOT open "
    "with filler and do NOT end with a grand closing. AVOID the over-polished "
    "tri-paragraph 'intro / body / call-to-action' shape. "
    "FORBIDDEN AI-slop tells (do NOT use these or close variants): «звучит "
    "интересно», «я в восторге», «был(а) бы рад(а) возможности», and excessive "
    "hedging. Write plainly and specifically instead.\n\n"
    "TARGET COMPANY: a ``company`` value may be provided in the JSON context "
    "below. When it is a real name (not null), you MAY address the отклик to "
    "that company so it reads as written FOR them (e.g. naming the company "
    "naturally in the opening). When company is null / not provided, write a "
    "neutral opening and do NOT write the literal word «None» / «null» / "
    "«компания» as a placeholder — simply omit the company clause.\n\n"
    "LINKS (do NOT fabricate URLs): proof of skill is the candidate's GitHub "
    "and резюме. A GitHub link ({github}) and a резюме reference are appended to "
    "the message AUTOMATICALLY after you write it, so you do NOT need to include "
    "them yourself — and you MUST NOT invent any other GitHub URL, portfolio "
    "URL, or a fake резюме/CV http link. Never output a made-up http(s) link for "
    "the resume. If you mention the GitHub or резюме in the body, refer to them "
    "in words only (e.g. «код на GitHub», «резюме во вложении») without "
    "inventing a URL."
)


def build_draft_system(profile: Profile) -> str:
    """Render the full DRAFT system prompt from the loaded profile. PURE.

    The de-slop / honesty / term-language / question RULES live in
    ``_DRAFT_SCAFFOLD`` (code); the candidate-SPECIFIC DATA (gender form,
    hands-on / in-development lists, GitHub link) is injected from ``profile``.
    """
    return (
        _DRAFT_SCAFFOLD
        .replace("{gender_rule}", _gender_rule(profile.gender))
        .replace("{hands_on}", _render_bullets(profile.hands_on))
        .replace("{in_development}", _render_bullets(profile.in_development))
        .replace("{github}", profile.draft_signature.github)
    )


# Module-level constant rendered from the GENERIC example profile so the module
# carries NO personal data. Live runs build the prompt from the loaded (local)
# profile via ``build_draft_system`` wired through the pipeline.
DRAFT_SYSTEM = build_draft_system(example_profile())


def build_draft_prompt(
    extracted: ExtractResult,
    raw_text: str,
    research: Optional[Dict[str, Any]] = None,
) -> str:
    payload: Dict[str, Any] = {
        "title": extracted.title,
        # The TARGET company the отклик is written FOR (may be null -> address
        # neutrally, never write the literal "None"). See DRAFT_SYSTEM.
        "company": extracted.company,
        "stack": extracted.stack,
        "contact_type": extracted.contact_type,
    }
    if research:
        payload["research"] = research
    company_line = (
        f"TARGET COMPANY (address the отклик to it): {extracted.company}\n"
        if extracted.company
        else "TARGET COMPANY: not named — use a neutral opening, do NOT write «None».\n"
    )
    return (
        "Write the application message for our (female) candidate.\n"
        + company_line
        + json.dumps(payload, ensure_ascii=False)
        + "\n\nRead the FULL vacancy text below; only ask about details it does "
        "NOT already answer, and drop the questions entirely if it covers "
        "everything relevant.\n\nFULL VACANCY TEXT (raw_text):\n" + raw_text
    )


# --- Impure orchestration wrappers (call the client) ------------------------


def llm_extract(
    client: LLMClient,
    raw_text: str,
    source_channel: str,
    source_link: Optional[str] = None,
    model: str = CHEAP_MODEL,
) -> ExtractResult:
    """Bulk/cheap extract -> routed to the cheap (Haiku) model."""
    text = client.complete(
        EXTRACT_SYSTEM,
        build_extract_prompt(raw_text, source_channel, source_link),
        max_tokens=EXTRACT_MAX_TOKENS,
        model=model,
        cache_system=should_cache_system(EXTRACT_SYSTEM, model),
    )
    return parse_extract_response(text, source_channel, source_link)


def llm_score(
    client: LLMClient,
    extracted: ExtractResult,
    raw_text: str,
    model: str = JUDGE_MODEL,
    profile: Optional[Profile] = None,
) -> Dict[str, Any]:
    """Rubric relevance score -> routed to the judgment (Sonnet) model.

    Returns {'score': int 0-100, 'reasoning': str (Обоснование)}.

    ``profile`` injects the candidate-specific rubric block. When None, the
    module default (rendered from the GENERIC example profile) is used — live
    runs pass the loaded local profile via the pipeline.
    """
    system = build_score_system(profile) if profile is not None else SCORE_SYSTEM
    text = client.complete(
        system,
        build_score_prompt(extracted, raw_text),
        max_tokens=SCORE_MAX_TOKENS,
        model=model,
        cache_system=should_cache_system(system, model),
    )
    return parse_score_response(text)


def llm_research(
    client: LLMClient,
    extracted: ExtractResult,
    raw_text: str,
    model: str = CHEAP_MODEL,
    fetched_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Research the company/role (T10) on the cheap (Haiku) model.

    When ``fetched_context`` carries real fetched page text it is added to the
    prompt in a clearly-labeled section so the model can ground `sourced_facts`
    in it. With no fetched context the prompt is byte-identical to before
    (desk-only fallback). ``max_tokens`` is RESEARCH_MAX_TOKENS to leave room
    for page-grounded `sourced_facts` and `talking_points` alongside the
    existing fields without truncating the JSON (the confirmed #20 failure).
    """
    text = client.complete(
        RESEARCH_SYSTEM,
        build_research_prompt(extracted, raw_text, fetched_context),
        max_tokens=RESEARCH_MAX_TOKENS,
        model=model,
        cache_system=should_cache_system(RESEARCH_SYSTEM, model),
    )
    return parse_research_response(text)


def llm_draft(
    client: LLMClient,
    extracted: ExtractResult,
    raw_text: str,
    research: Optional[Dict[str, Any]] = None,
    model: str = CHEAP_MODEL,
    profile: Optional[Profile] = None,
) -> str:
    """Generate the отклик body. ``profile`` injects the gender form, hands-on /
    in-development lists and the GitHub link. When None the module default
    (GENERIC example profile) is used."""
    system = build_draft_system(profile) if profile is not None else DRAFT_SYSTEM
    return client.complete(
        system,
        build_draft_prompt(extracted, raw_text, research),
        max_tokens=DRAFT_MAX_TOKENS,
        model=model,
        cache_system=should_cache_system(system, model),
    ).strip()
