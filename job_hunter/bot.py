"""aiogram notification bot — the HITL surface.

Sends surfaced jobs to the operator with inline buttons that drive
pipeline.advance(decision=...): approve / skip / backlog at the surface gate,
and draft -> send at the draft gate.

Pure helpers (callback encode/decode, message rendering, keyboard building) are
split out and unit-tested. The async aiogram wiring requires BOT_TOKEN and is
covered by mocked tests only.

Run the bot:
    python -m job_hunter.bot
"""

from __future__ import annotations

import html
import json
import re
from typing import Any, List, Optional, Set, Tuple

import psycopg

from . import add_by_url as add_by_url_mod
from . import fx as fx_mod
from . import pipeline, research_fetch, store
from .config import Config, load_config
from .pipeline import Deps
from .schema_extract import from_dict
from .states import (
    DECISION_APPROVE, DECISION_BACKLOG, DECISION_CLOSE, DECISION_DECLINE,
    DECISION_INTERVIEW, DECISION_OFFER, DECISION_SCREENING, DECISION_SEND,
    DECISION_SKIP, DRAFTED, SURFACED, allowed_transitions,
)

CALLBACK_PREFIX = "jh"

# A keyboard button spec. Either:
#   (label, callback_data, style)                 -- a callback button, OR
#   (label, callback_data, style, copy_text)      -- callback OR copy_text button
# callback_data and copy_text are mutually exclusive (one is None per button).
# The 3-tuple form is accepted for back-compat (copy_text defaults to None).
ButtonSpec = Tuple  # documented above; tuples of length 3 or 4


# --- Pure: callback data ----------------------------------------------------


def encode_callback(action: str, item_id: int) -> str:
    """Encode an inline-button callback payload. PURE."""
    return f"{CALLBACK_PREFIX}:{action}:{item_id}"


def decode_callback(data: str) -> Optional[Tuple[str, int]]:
    """Decode a callback payload -> (action, item_id) or None. PURE."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    action = parts[1]
    try:
        item_id = int(parts[2])
    except ValueError:
        return None
    return action, item_id


# --- Pure: URL extraction (add-by-URL bot handler) --------------------------


def first_url(text: Optional[str]) -> Optional[str]:
    """Return the FIRST http(s) URL in a message, or None. PURE, never raises.

    Reuses ``research_fetch``'s URL regex + trailing-punctuation trim so a link
    pasted with surrounding prose (or a trailing period) is recognized exactly as
    the ingest path recognizes in-body apply URLs. A t.me link counts — it is
    just another source.
    """
    match = research_fetch._URL_RE.search(text or "")
    if not match:
        return None
    return research_fetch._strip_url_trailing(match.group(0)) or None


# --- Pure: access control ---------------------------------------------------


def is_allowed(user_id: Optional[int], allowed: Set[int]) -> bool:
    """Return True iff ``user_id`` is in the allowlist. PURE.

    Fails closed: an unknown user id (None) or an empty allowlist => False.
    """
    if user_id is None:
        return False
    return user_id in allowed


def _user_id_of(update: Any) -> Optional[int]:
    """Extract the originating Telegram user id from a message or callback
    query, or None if it cannot be determined. PURE (just attribute access)."""
    from_user = getattr(update, "from_user", None)
    if from_user is None:
        return None
    uid = getattr(from_user, "id", None)
    if uid is None:
        return None
    try:
        return int(uid)
    except (TypeError, ValueError):
        return None


# Maps button action -> pipeline decision. The post-send funnel actions reuse
# the decision string AS the action token (screening/interview/offer/decline/
# close), so encode_callback(decision, id) round-trips through decode_callback.
ACTION_TO_DECISION = {
    "approve": DECISION_APPROVE,
    "skip": DECISION_SKIP,
    "backlog": DECISION_BACKLOG,
    "send": DECISION_SEND,
    "screening": DECISION_SCREENING,
    "interview": DECISION_INTERVIEW,
    "offer": DECISION_OFFER,
    "decline": DECISION_DECLINE,
    "close": DECISION_CLOSE,
}


# --- Pure: language detection -----------------------------------------------

_CYRILLIC_RE = __import__("re").compile(r"[Ѐ-ӿ]")


def is_russian(text: Optional[str]) -> bool:
    """True when the text contains Cyrillic characters. PURE (simple heuristic)."""
    if not text:
        return False
    return bool(_CYRILLIC_RE.search(text))


# Per-language field labels for the card.
_LABELS = {
    "ru": {
        "company": "Компания",
        "stack": "Стек",
        "seniority": "Уровень",
        "remote": "Remote",
        "salary": "Зарплата",
        "benefits": "Условия",
        "contact": "Контакт",
        "source": "Источник",
        "reasoning": "💭 Обоснование:",
        "open": "🔗 Открыть оригинал",
        "salary_unknown": "не указана",
        "yes": "да",
        "no": "нет",
        "unknown": "?",
    },
    "en": {
        "company": "Company",
        "stack": "Stack",
        "seniority": "Level",
        "remote": "Remote",
        "salary": "Salary",
        "benefits": "Perks",
        "contact": "Contact",
        "source": "Source",
        "reasoning": "💭 Rationale:",
        "open": "🔗 Open original",
        "salary_unknown": "unknown",
        "yes": "yes",
        "no": "no",
        "unknown": "?",
    },
}

# Score-band emoji cutoffs (applied to the 0..100 relevance score).
SCORE_BAND_HIGH = 75   # >= -> 🟢
SCORE_BAND_MID = 60    # >= -> 🟡, else -> 🔴


def score_emoji(score: Optional[float]) -> str:
    """Pick the header emoji by score band. PURE."""
    if score is None:
        return "🔴"
    if score >= SCORE_BAND_HIGH:
        return "🟢"
    if score >= SCORE_BAND_MID:
        return "🟡"
    return "🔴"


# --- Pure: rendering --------------------------------------------------------


def _esc(value: Any) -> str:
    """HTML-escape a dynamic value for Telegram HTML parse_mode. PURE.

    Escapes ``< > &`` so user/extracted text never breaks the HTML markup or is
    misread as a tag. ``html.escape`` also handles quotes; ``quote=False`` keeps
    quotes literal (they are safe outside attribute values and read cleaner).
    """
    return html.escape("" if value is None else str(value), quote=False)


def _b(label: str) -> str:
    """Wrap a (static) field label in Telegram HTML bold. PURE.

    Labels are our own constants (never user input), so they are bolded as-is.
    """
    return f"<b>{label}</b>"


def _yesno(v: Optional[bool], lang: dict) -> str:
    if v is True:
        return lang["yes"]
    if v is False:
        return lang["no"]
    return lang["unknown"]


def _salary_text(ex, salary_rub: Optional[float], lang: dict) -> str:
    """Original amount + currency, with the ₽-equivalent SIDE BY SIDE. PURE.

    The original is never replaced; the ₽-equiv is appended in parentheses when
    available.
    """
    if ex.salary_min is None and ex.salary_max is None:
        return lang["salary_unknown"]
    lo, hi, cur = ex.salary_min, ex.salary_max, ex.currency or ""
    if lo is not None and hi is not None and lo != hi:
        orig = f"{lo:g}-{hi:g} {cur}".strip()
    else:
        orig = f"{(hi if hi is not None else lo):g} {cur}".strip()
    rub = fx_mod.format_rub(salary_rub) if salary_rub is not None else ""
    return f"{orig} ({rub})" if rub else orig


def render_surfaced(item: store.WorkItem, salary_rub: Optional[float] = None) -> str:
    """Render a surfaced job card. PURE.

    Card language follows the POST's language (Russian when the post text is
    Russian, else English). Layout:
      <emoji> <score>/100 — <title>
      <Company/Stack/Level/Remote/Salary/Contact/Source labels>
      💭 Обоснование: <Sonnet rationale, post language>
      🔗 Открыть оригинал
    """
    try:
        data = json.loads(item.extracted_json or "{}")
    except Exception:
        data = {}
    ex = from_dict(data)

    # Language from the original post text (fallback to extracted fields).
    sample = (item.raw_text or "") + " " + (ex.title or "")
    lang = _LABELS["ru"] if is_russian(sample) else _LABELS["en"]

    score = item.relevance_score
    score_str = f"{score:.0f}" if score is not None else "?"
    # Header zone: emoji band + score/100 + title. Title is dynamic -> escaped.
    header = f"{score_emoji(score)} {score_str}/100 — {_esc(ex.title)}".rstrip(" —")

    # --- Zone 1: header --------------------------------------------------
    lines: List[str] = [header]

    # --- Zone 2: fields (bold labels, escaped dynamic values) ------------
    field_lines: List[str] = []
    if ex.company:
        field_lines.append(f"{_b(lang['company'])}: {_esc(ex.company)}")
    if ex.stack:
        field_lines.append(f"{_b(lang['stack'])}: " + _esc(", ".join(ex.stack)))
    if ex.seniority:
        field_lines.append(f"{_b(lang['seniority'])}: {_esc(ex.seniority)}")
    field_lines.append(f"{_b(lang['remote'])}: {_esc(_yesno(ex.remote, lang))}")
    field_lines.append(f"{_b(lang['salary'])}: {_esc(_salary_text(ex, salary_rub, lang))}")
    # DISPLAY-ONLY perks/conditions. Omitted entirely when empty. No scoring use.
    if ex.benefits:
        field_lines.append(f"{_b(lang['benefits'])}: " + _esc(", ".join(ex.benefits)))
    if ex.contact:
        ctype = ex.contact_type or lang["unknown"]
        field_lines.append(f"{_b(lang['contact'])} ({_esc(ctype)}): {_esc(ex.contact)}")
    if ex.source_channel or ex.source_link:
        src = ex.source_channel or ""
        if ex.source_link:
            src = f"{src} {ex.source_link}".strip()
        field_lines.append(f"{_b(lang['source'])}: {_esc(src)}")
    if field_lines:
        lines.append("")
        lines.extend(field_lines)

    # --- Zone 3: verdict (Обоснование, Sonnet rationale) -----------------
    # Reserved top-level key, fallback to the first deterministic reason.
    reasoning = data.get("Обоснование")
    if not reasoning and ex.reasons:
        reasoning = ex.reasons[0]
    if reasoning:
        lines.append("")
        lines.append("─────────")
        # The label dict value already carries its emoji prefix + trailing ':'.
        # Bold the whole verdict label; the rationale body is escaped (it may
        # contain post text with < > &), preserving its own newlines.
        lines.append(_b(lang["reasoning"]))
        lines.append(_esc(reasoning))

    # --- Zone 4: link (preview disabled on the send) ---------------------
    if ex.source_link:
        lines.append("")
        lines.append(f'{_b(lang["open"])}: {_esc(ex.source_link)}')

    return "\n".join(lines)


# Empty-band reply for /borderline (exact one-line string, Russian).
BORDERLINE_EMPTY = "нет пограничных вакансий (50-59)"
# Keep the /borderline reply safely under Telegram's 4096-char message limit;
# beyond this the renderer stops and notes the remainder ("…и ещё N").
_BORDERLINE_CHAR_BUDGET = 3900


def _borderline_company_title(item: store.WorkItem) -> Tuple[str, str]:
    """Parse (company, title) out of an item's extracted_json. PURE.

    Tolerant of missing/garbage JSON (returns ("", "") components missing) so a
    half-extracted borderline row never breaks the list. Reuses schema_extract.
    """
    try:
        ex = from_dict(json.loads(item.extracted_json or "{}"))
    except Exception:
        ex = from_dict({})
    return (ex.company or ""), (ex.title or "")


# Per-⚠️-bullet soft trim and the joined-reason hard cap (chars). The per-bullet
# trim keeps multiple blockers visible; the total cap bounds the inline reason
# line so a card block stays compact on the /borderline list.
_BORDERLINE_BULLET_MAX = 60
_BORDERLINE_REASON_MAX = 120
# A word-trimmed bullet shorter than this carries no real signal (e.g. a 3-char
# "сло…" stub). Used as an explicit floor when trimming a SINGLE bullet down to
# the reason cap so we never emit a meaningless fragment.
_BORDERLINE_MIN_BULLET_CHARS = 15
# Characters stripped off the tail of a word-trimmed body BEFORE the ellipsis,
# so a cut never leaves a dangling comma/dash or a lone opening quote/bracket
# ("…без оговорки «" -> "…без оговорки…"). Includes whitespace, sentence
# punctuation, dashes, both guillemets, straight/curly quotes, brackets, and a
# pre-existing U+2026 ellipsis (so a bullet already ending in "…" never yields
# a double "……" when we append our own ellipsis).
_TRIM_TRAILING = " \t\n,.;:!?—–-«»\"'“”‘’()[]{}…"
# Split the stored «Обоснование» on the rationale markers. ✅ flags a positive
# (dropped); ⚠️ flags a concern/hard-flag (kept). The captured group lets us
# track WHICH marker preceded each segment so only ⚠️ segments survive.
# NOTE: ⚠️ is TWO codepoints (U+26A0 WARNING SIGN + U+FE0F VARIATION SELECTOR);
# a bare character class would split off only U+26A0 and leave the selector on
# the segment, so the markers are matched as whole sequences here (the trailing
# selector is optional so a plain U+26A0 without it is still recognised).
_REASON_MARKER_RE = re.compile("(⚠️?|✅)")


def _soft_trim(text: str, limit: int, min_chars: int = 0) -> str:
    """Truncate ``text`` to ``limit`` chars on a WORD boundary, appending '…'. PURE.

    When ``text`` is longer than ``limit`` the ellipsis must fit too, so the body
    budget is ``limit - 1`` chars. Within that budget we cut at the LAST whole
    word (the last space), then strip any trailing punctuation / space / opening
    or closing quote/bracket (see ``_TRIM_TRAILING``) before appending '…' — so
    "…опыта в разработке…" trims to "…опыта в…" not "…в разрабо…", and
    "…оговорки «желательно»" trims to "…оговорки…" not "…оговорки «…".

    FALLBACK: if there is NO space inside the budget (a single token longer than
    the limit), hard-cut to ``limit - 1`` chars + '…' so a giant unbroken token
    still fits. ``min_chars`` (default 0) is a floor on the word-trimmed body: if
    the whole-word cut would leave fewer than ``min_chars`` chars (a degenerate
    "short word + giant token" case that would otherwise emit a 3-char "сло…"
    stub) we instead hard-cut to fill the budget. The result length is always
    <= ``limit``.
    """
    if len(text) <= limit:
        return text
    budget = text[: limit - 1]
    hard = (budget.rstrip(_TRIM_TRAILING) or budget) + "…"
    cut = budget.rfind(" ")
    if cut <= 0:
        # No usable space within the budget: a single oversized token. Hard-cut
        # (strip a trailing dangling char first so we don't end on punctuation).
        return hard
    body = budget[:cut].rstrip(_TRIM_TRAILING)
    if not body or len(body) < min_chars:
        # Either everything before the cut was punctuation/quotes, OR the
        # whole-word cut leaves too little signal: pack the budget via hard-cut.
        return hard
    return body + "…"


def _borderline_reason(item: store.WorkItem) -> str:
    """Build a compact one-line "why below threshold" from stored rationale. PURE.

    Reads the ALREADY-STORED «Обоснование» (pipeline.REASONING_KEY) off the
    item's extracted_json — NO LLM call, NO re-score. Keeps only the ⚠️ concern
    bullets (the verdict line and all ✅ positives are dropped), condenses each
    (whitespace collapsed, trailing period stripped, WORD-trimmed to
    ~``_BORDERLINE_BULLET_MAX`` chars), then GREEDILY joins WHOLE bullets with
    " · " while the running line stays <= ``_BORDERLINE_REASON_MAX`` — it STOPS
    at the first bullet that would not fit rather than truncating one mid-way, so
    the line reads as clean whole phrases (one full bullet is preferred over two
    mangled fragments). A first bullet that alone exceeds the reason cap is
    word-trimmed down to it. Returns "" when the rationale is missing/garbage OR
    has no ⚠️ bullets — the renderer then OMITS the reason line (never invents a
    blocker).

    The rationale may be newline-separated OR have the bullets inline, so the
    split is on the ✅/⚠️ MARKERS (not on newlines), tracking the marker that
    preceded each segment.
    """
    # Same tolerant parse as _borderline_company_title: missing/garbage -> "".
    try:
        reasoning = json.loads(item.extracted_json or "{}").get(pipeline.REASONING_KEY)
    except Exception:
        return ""
    if not reasoning or not isinstance(reasoning, str):
        return ""

    # Split on the markers; re.split with a capturing group yields alternating
    # [pre-marker-text, marker, segment, marker, segment, ...]. The leading
    # element (before the first marker) is the verdict line and is discarded.
    parts = _REASON_MARKER_RE.split(reasoning)
    concerns: List[str] = []
    # Walk (marker, segment) pairs after the discarded leading verdict text.
    for i in range(1, len(parts) - 1, 2):
        marker = parts[i]
        segment = parts[i + 1]
        # Compare on the base codepoint: the warning marker may arrive as ⚠️
        # (with the U+FE0F variation selector) or a bare ⚠ (U+26A0).
        if not marker.startswith("⚠"):
            continue  # ✅ positives (and anything not a ⚠️) are dropped
        # Drop any leftover U+FE0F variation selector at the segment start, then
        # condense: collapse internal whitespace/newlines, drop trailing period.
        segment = segment.lstrip("️")
        condensed = " ".join(segment.split()).rstrip(".").strip()
        if not condensed:
            continue
        concerns.append(
            _soft_trim(condensed, _BORDERLINE_BULLET_MAX,
                       min_chars=_BORDERLINE_MIN_BULLET_CHARS)
        )

    if not concerns:
        return ""

    # STUB-AVOIDANCE: assemble WHOLE (already bullet-trimmed) bullets only, never
    # a mid-bullet fragment. Greedily append " · <next>" while the running line
    # stays <= _BORDERLINE_REASON_MAX; STOP at the first bullet that would not
    # fit (do NOT add a fragment of it). Two ~60-char bullets (60+3+60=123 > 120)
    # thus show only the first, cleanly; several short bullets show as many whole
    # ones as fit.
    line = concerns[0]
    if len(line) > _BORDERLINE_REASON_MAX:
        # First bullet alone overflows the reason cap: word-trim it down to the
        # cap, with the ≥15-char floor so a degenerate cut packs the budget via
        # hard-cut rather than emitting a 3-char "сло…" stub.
        return _soft_trim(line, _BORDERLINE_REASON_MAX,
                          min_chars=_BORDERLINE_MIN_BULLET_CHARS)
    for nxt in concerns[1:]:
        candidate = f"{line} · {nxt}"
        if len(candidate) > _BORDERLINE_REASON_MAX:
            break  # whole bullets only: drop this one rather than fragment it
        line = candidate
    return line


def render_borderline(items: List[store.WorkItem]) -> str:
    """Render the COMPACT /borderline list. PURE.

    READ-ONLY browse view of cards scoring in [50, 60). Multi-line card block:

        52 — Bell Integrator · AI/LLM Инженер
           ⚠️ senior-грейд · обязательный диплом
           https://t.me/...

    Per card: line 1 = ``<score> — <company> · <title>`` (score TRUNCATED via
    int()); line 2 (ONLY when ``_borderline_reason`` is non-empty) = an indented
    ``   ⚠️ <reason>`` condensed from the stored «Обоснование» concerns; line 3
    (ONLY when a link is present) = the indented link. NO LLM call / no re-score
    — the reason comes purely from already-stored data.

    Items arrive already ordered by the store (relevance_score DESC NULLS LAST,
    created_at DESC), i.e. highest-score-first then newest. No action buttons.
    Empty band -> the exact one-liner ``BORDERLINE_EMPTY``.

    No HTML markup is used (plain text), so values are NOT escaped — the message
    is sent without parse_mode and with link previews disabled.
    """
    if not items:
        return BORDERLINE_EMPTY

    blocks: List[str] = []
    total = 0
    for idx, item in enumerate(items):
        company, title = _borderline_company_title(item)
        score = item.relevance_score
        # TRUNCATE (not round): a 59.x borderline item must read "59", never
        # "60" — it would otherwise look like it cleared SURFACE_THRESHOLD.
        score_str = f"{int(score)}" if score is not None else "?"
        facts = " · ".join(p for p in (company, title) if p)
        head = f"{score_str} — {facts}" if facts else score_str

        block_lines = [head]
        reason = _borderline_reason(item)
        if reason:
            block_lines.append(f"   ⚠️ {reason}")
        link = item.source_link or ""
        if link:
            block_lines.append(f"   {link}")
        block = "\n".join(block_lines)

        # Bound the message under Telegram's 4096-char limit: stop before the
        # budget and note the remainder rather than letting the send fail. The
        # budget is now BLOCK-aware (each card may span multiple lines).
        if blocks and total + len(block) + 1 > _BORDERLINE_CHAR_BUDGET:
            blocks.append(f"…и ещё {len(items) - idx} (открой дашборд)")
            break
        blocks.append(block)
        total += len(block) + 1
    return "\n".join(blocks)


def _render_worth(result, age: int) -> str:
    """Render a MarketWorthResult as a Telegram-ready text block. PURE."""
    from .market_worth import fmt_range

    ru   = fmt_range(result.ru_min, result.ru_max, result.ru_currency)
    intl = fmt_range(result.intl_min, result.intl_max, result.intl_currency)

    lines = [
        "📊 <b>Зарплата по рынку</b>",
        "",
        f"🇷🇺 Россия:       {ru}",
        f"🌍 Международный: {intl}",
        "",
        result.reasoning_short,
    ]
    if result.sources:
        lines += ["", "<b>Источники:</b>"]
        for s in result.sources[:5]:
            lines.append(f"  • {s}")
    freshness = f"обновлено {age} д. назад" if age > 0 else "обновлено сегодня"
    if result.degraded:
        freshness += f" | ⚠️ {result.degraded_reason}"
    lines += ["", f"<i>{freshness}</i>"]
    return "\n".join(lines)


def render_draft(item: store.WorkItem) -> str:
    """Render a drafted item (with the generated message) for review. PURE."""
    try:
        data = json.loads(item.extracted_json or "{}")
    except Exception:
        data = {}
    draft = data.get("draft") or "(no draft text)"
    title = data.get("title") or "(untitled)"
    return f"#{item.id}  DRAFT for: {title}\n\n{draft}"


# Final-state confirmation lines shown on the card once an action is taken.
# Keyed by the pipeline decision; Russian copy (the operator-facing language).
# Used by handle_callback to EDIT the card after a button press so the card no
# longer looks actionable (the inline keyboard is removed at the same time).
_FINAL_STATE_LINE = {
    DECISION_APPROVE: "✅ Принято",
    DECISION_BACKLOG: "📥 В бэклоге",
    DECISION_SKIP: "⏭️ Пропущено",
    DECISION_SEND: "✅ Отправлено",
    # Post-send response funnel. For non-terminal stages (screening/interview)
    # this line is appended AND the card keeps the next-step keyboard.
    DECISION_SCREENING: "📞 Ответили / скрининг",
    DECISION_INTERVIEW: "🗣️ Собес",
    DECISION_OFFER: "🎉 Оффер!",
    DECISION_DECLINE: "❌ Отказ работодателя",
    DECISION_CLOSE: "🗄️ Закрыто",
}


def final_state_line(decision: str) -> Optional[str]:
    """Return the final-state confirmation line for a decision, or None. PURE."""
    return _FINAL_STATE_LINE.get(decision)


# Bot API 9.4 inline-button colors. Mapped per action so a colour-aware client
# renders Approve green / Skip red / Backlog blue. Clients that ignore the
# field fall back to the emoji prefix baked into the label (see specs below).
BUTTON_STYLE_SUCCESS = "success"  # green
BUTTON_STYLE_DANGER = "danger"    # red
BUTTON_STYLE_PRIMARY = "primary"  # blue


def surfaced_keyboard_spec(item_id: int) -> List[Tuple[str, str, Optional[str]]]:
    """Return [(label, callback_data, style)] for the surface gate. PURE.

    Each label carries an emoji prefix as a colour FALLBACK for clients that do
    not render the Bot API 9.4 ``style`` field. callback_data is UNCHANGED
    (handlers/allowlist still decode the same action tokens).
    """
    return [
        ("✅ Approve", encode_callback("approve", item_id), BUTTON_STYLE_SUCCESS),
        ("📥 Backlog", encode_callback("backlog", item_id), BUTTON_STYLE_PRIMARY),
        ("⏭️ Skip", encode_callback("skip", item_id), BUTTON_STYLE_DANGER),
    ]


# Telegram copy_text button payload caps at 256 chars (Bot API). A contact
# longer than this cannot be a copy button, so we omit the button entirely.
COPY_TEXT_MAX = 256


def draft_keyboard_spec(
    item_id: int, contact: Optional[str] = None
) -> List["ButtonSpec"]:
    """Return the draft-gate keyboard spec. PURE.

    The отклик is sent MANUALLY by the operator (contacts vary: Telegram DM /
    email / web form — there is NO universal auto-send). So the keyboard does
    NOT send anything anywhere. It only provides:

      * «✅ Отправила» — a MANUAL-CONFIRM the operator taps AFTER they have sent
        the отклик themselves. callback_data uses the stable ``send`` action
        token (ACTION_TO_DECISION['send'] -> DECISION_SEND -> T12 DRAFTED->SENT,
        the SAME state transition as before; only the label/UX changed).
      * «📋 Контакт» — OPTIONAL. A Telegram ``copy_text`` button that copies the
        extracted contact (email / @handle / link) to the clipboard. Included
        ONLY when ``contact`` is a non-empty string of <= COPY_TEXT_MAX chars
        (the Bot API copy_text cap). Omitted entirely otherwise. It carries NO
        callback_data — copy_text buttons are handled client-side and never hit
        a handler.

    Each ButtonSpec is (label, callback_data, style, copy_text); callback_data
    and copy_text are mutually exclusive per button.
    """
    spec: List[ButtonSpec] = [
        # MANUAL-CONFIRM: affirmative -> success (green) + ✅ fallback emoji.
        (
            "✅ Отправила",
            encode_callback("send", item_id),
            BUTTON_STYLE_SUCCESS,
            None,
        ),
    ]
    if contact is not None:
        c = contact.strip()
        if c and len(c) <= COPY_TEXT_MAX:
            # copy_text button: no callback_data, no style; copy_text carries
            # the clipboard payload (serialised as {"text": c} to the Bot API).
            spec.append(("📋 Контакт", None, None, c))
    return spec


# Post-send response funnel buttons. decision -> (label, style). Labels are the
# human-readable button text; the style is the Bot API 9.4 colour for clients
# that render it. SAME action token == decision string (see ACTION_TO_DECISION).
FUNNEL_DECISION_LABELS = {
    DECISION_SCREENING: ("Ответили / скрининг", BUTTON_STYLE_PRIMARY),
    DECISION_INTERVIEW: ("Собес", BUTTON_STYLE_PRIMARY),
    DECISION_OFFER: ("Оффер 🎉", BUTTON_STYLE_SUCCESS),
    DECISION_DECLINE: ("Отказ", BUTTON_STYLE_DANGER),
    DECISION_CLOSE: ("Закрыть", BUTTON_STYLE_DANGER),
}


def funnel_keyboard_spec(state: str, item_id: int) -> List[Tuple[str, str, Optional[str]]]:
    """Buttons for the post-send funnel, derived from the state machine. PURE.

    Reads ``states.allowed_transitions(state)`` and renders one button per manual
    funnel decision legal from ``state`` (screening/interview/offer/decline/
    close). Returns [] for any state with no funnel actions (surfaced/drafted/
    terminal), so callers can treat an empty spec as "nothing to re-stage".
    """
    spec: List[Tuple[str, str, Optional[str]]] = []
    for t in allowed_transitions(state):
        entry = FUNNEL_DECISION_LABELS.get(t.decision)
        if entry is None:
            continue
        label, style = entry
        spec.append((label, encode_callback(t.decision, item_id), style))
    return spec


# --- aiogram wiring (impure) ------------------------------------------------


def _no_preview():
    """LinkPreviewOptions that disables the web-page preview (aiogram 3.x).

    Keeps the card compact: the big original-post preview is suppressed even
    though the «Открыть оригинал» link is present in the body.
    """
    from aiogram.types import LinkPreviewOptions

    return LinkPreviewOptions(is_disabled=True)


def _build_button(
    label: str,
    data: Optional[str],
    style: Optional[str],
    copy_text: Optional[str] = None,
):
    """Build one InlineKeyboardButton, carrying Bot API extras via passthrough.

    The installed aiogram (3.13.x, pinned because newer releases require
    Python >= 3.10 and this interpreter is 3.9) has no native ``style`` field on
    InlineKeyboardButton, and no native ``CopyTextButton`` / ``copy_text`` field.
    Its pydantic model is configured ``extra='allow'``, so BOTH are accepted as
    extra attributes and ARE included in the JSON aiogram serialises to the Bot
    API.

    - ``style`` (Bot API 9.4) -> serialised as a plain string field.
    - ``copy_text`` (Bot API 7.11 CopyTextButton) -> the Bot API expects an
      OBJECT ``{"text": "<payload>"}``; we pass exactly that dict so it
      serialises to the correct shape. A copy_text button has NO callback_data.
    """
    from aiogram.types import InlineKeyboardButton

    kwargs: dict = {"text": label}
    if data is not None:
        kwargs["callback_data"] = data
    if style is not None:
        kwargs["style"] = style  # passthrough extra field -> serialised to API
    if copy_text is not None:
        # Bot API CopyTextButton shape: {"text": "..."}. Passthrough extra.
        kwargs["copy_text"] = {"text": copy_text}
    return InlineKeyboardButton(**kwargs)


def _unpack_spec(b: ButtonSpec) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """Normalise a 3- or 4-tuple button spec to (label, data, style, copy_text)."""
    if len(b) == 4:
        return b[0], b[1], b[2], b[3]
    label, data, style = b
    return label, data, style, None


def _build_keyboard(spec: List[ButtonSpec]):
    from aiogram.types import InlineKeyboardMarkup

    row = [_build_button(*_unpack_spec(b)) for b in spec]
    return InlineKeyboardMarkup(inline_keyboard=[row])


class JobHunterBot:
    """Encapsulates the aiogram Bot/Dispatcher + a DB connection + Deps."""

    def __init__(self, cfg: Config, conn: psycopg.Connection, deps: Deps) -> None:
        self.cfg = cfg
        self.conn = conn
        self.deps = deps
        self._bot = None
        self._dp = None
        self._closed = False

    def _ensure(self):
        if self._bot is not None:
            return
        from aiogram import Bot, Dispatcher

        self.cfg.require("bot_token", "notify_chat_id")
        self._bot = Bot(token=self.cfg.bot_token)
        self._dp = Dispatcher()
        self._register()

    def _register(self) -> None:
        from aiogram import F
        from aiogram.filters import Command
        from aiogram.types import CallbackQuery, Message

        # Centralized access-control gate. Registered as an OUTER middleware on
        # both the message and callback_query observers so it runs before ANY
        # handler, filter, or business logic — a new handler is covered
        # automatically and cannot forget the check. See module/class docstring.
        gate = self._make_access_gate()
        self._dp.message.outer_middleware(gate)
        self._dp.callback_query.outer_middleware(gate)

        @self._dp.callback_query(F.data.startswith(CALLBACK_PREFIX + ":"))
        async def on_callback(cb: CallbackQuery) -> None:  # noqa: ANN001
            await self.handle_callback(cb)

        # READ-ONLY /borderline command. Registered on the MESSAGE observer, so
        # the existing access_gate OUTER middleware (already attached to
        # self._dp.message above) auto-gates it to the allowlist — non-allowlisted
        # senders are dropped BEFORE this handler runs. The handler only SELECTs
        # and sends text; it never advances/updates state.
        @self._dp.message(Command("borderline"))
        async def on_borderline(message: Message) -> None:  # noqa: ANN001
            await self.handle_borderline(message)

        # Market Worth: /worth or /worth refresh
        @self._dp.message(Command("worth"))
        async def on_worth(message: Message) -> None:  # noqa: ANN001
            await self.handle_worth(message)

        # Add-a-vacancy-by-URL: any text message carrying an http(s) link is run
        # through the SAME add-by-URL flow as the dashboard input, then routed by
        # score band. Registered AFTER the /borderline command so the command
        # wins its match; this catch-all only sees non-command text and ignores
        # anything without a URL. The access_gate OUTER middleware already
        # allowlist-gates it (non-allowlisted senders never reach here).
        @self._dp.message(F.text)
        async def on_text(message: Message) -> None:  # noqa: ANN001
            await self.handle_url_message(message)

        # --- Ops-channel plumbing (Part B) -------------------------------
        # Wire the ops startup ping + global error handler onto THIS existing
        # dispatcher (never a second Dispatcher). The handler bodies live as
        # bound methods (on_startup / on_error) so they are directly unit-
        # testable; here we just register them on the dispatcher's startup and
        # errors observers.
        self._dp.startup.register(self.on_startup)
        self._dp.errors.register(self.on_error)

    def _make_access_gate(self):
        """Build the aiogram outer middleware that enforces the allowlist.

        Drops (does not call ``handler``) any update whose from-user id is not
        in ``cfg.allowed_user_ids``. For callback queries it answers with an
        empty ack to stop Telegram's spinner, but runs NO business logic.
        """

        async def access_gate(handler, event, data):  # noqa: ANN001
            if not is_allowed(_user_id_of(event), self.cfg.allowed_user_ids):
                # Dropped: do not invoke the handler. Best-effort silence the
                # callback spinner without leaking any logic.
                answer = getattr(event, "answer", None)
                if answer is not None and getattr(event, "data", None) is not None:
                    try:
                        await answer()
                    except Exception:
                        pass
                return None
            return await handler(event, data)

        return access_gate

    # --- Ops-channel hooks (Part B) -----------------------------------------

    async def on_startup(self) -> None:
        """aiogram @dp.startup() hook: ping the ops channel when polling starts.

        SHORT_SHA is injected at image BUILD time (.dockerignore excludes .git,
        so it cannot be read from git at runtime inside the container); defaults
        to "unknown" for local runs. ``tg_logger.send_log`` no-ops gracefully
        when the ops vars are unset and never raises.
        """
        import os as _os

        from . import tg_logger

        short_sha = _os.environ.get("GIT_SHA", "unknown")
        await tg_logger.send_log(f"✅ jobhunter поднялся {short_sha}")

    async def on_error(self, event) -> bool:  # noqa: ANN001
        """aiogram @dp.errors() global handler: report unhandled errors to ops.

        Receives an ErrorEvent whose ``exception`` is the unhandled error (we
        fall back to the event itself if shaped otherwise). The send is
        DEBOUNCED in tg_logger so a flapping update cannot flood the ops topic,
        and never raises. Returns True to mark the error handled so it does not
        crash the polling loop.
        """
        from . import tg_logger

        exc = getattr(event, "exception", event)
        print(f"[error] unhandled handler exception: {exc!r}", flush=True)
        await tg_logger.send_error_log(exc)
        return True

    async def notify_text(self, text: str) -> None:
        """Send a plain one-line text message to the operator's notify chat.

        Used by harvest to report a completed run that surfaced ZERO new cards
        (otherwise silence is ambiguous). Mirrors the other notify_* methods:
        ``_ensure`` lazily builds the Bot, then send to ``cfg.notify_chat_id``
        with the link preview disabled. Carries no keyboard and writes no state.
        """
        self._ensure()
        await self._bot.send_message(
            self.cfg.notify_chat_id,
            text,
            disable_web_page_preview=True,
        )

    # Half-open borderline band [BORDERLINE_MIN, BORDERLINE_MAX): 50..59.
    BORDERLINE_MIN = 50.0
    BORDERLINE_MAX = 60.0  # exclusive -> 60 is SURFACE_THRESHOLD, stays surfaced

    async def handle_borderline(self, message) -> None:  # noqa: ANN001
        """READ-ONLY /borderline: list cards scoring [50, 60), highest-first.

        Queries ``work_items`` for relevance_score in the half-open band
        [50, 60) REGARDLESS of state (borderline cards usually sit in
        'rejected'), renders the COMPACT list, and replies to the requesting
        chat with link previews disabled. It NEVER advances, updates state,
        surfaces, drafts, or sends a card — it only SELECTs and answers text.

        The allowlist is enforced by the existing access_gate OUTER middleware on
        self._dp.message; non-allowlisted senders are dropped before this runs.
        """
        items = store.list_pipeline(
            self.conn,
            min_score=self.BORDERLINE_MIN,
            max_score=self.BORDERLINE_MAX,
        )
        text = render_borderline(items)
        await message.answer(
            text,
            link_preview_options=_no_preview(),
        )

    async def handle_worth(self, message) -> None:  # noqa: ANN001
        """/worth [refresh] — show (or force-refresh) the market salary benchmark.

        /worth          → return cached result, or refresh if no cache exists.
        /worth refresh  → force a web-search refresh regardless of cache age.

        The refresh call is slow (~10-30 s); bot sends a "loading…" message
        first and edits it with the result when the search completes.
        """
        from .market_worth import age_days, fmt_range, get_or_refresh, is_stale, load_cache
        from .profile import load_profile

        text = getattr(message, "text", "") or ""
        force = text.strip().lower().endswith("refresh")

        profile = load_profile()

        if not force:
            cached = load_cache(self.cfg.market_worth_cache_path)
            if cached is not None and not is_stale(cached, self.cfg.market_worth_cache_days):
                await message.answer(
                    _render_worth(cached, age_days(cached)),
                    parse_mode="HTML",
                    link_preview_options=_no_preview(),
                )
                return

        sent = await message.answer("⏳ Ищу данные по рынку…")
        try:
            import asyncio as _asyncio
            result = await _asyncio.get_event_loop().run_in_executor(
                None,
                lambda: get_or_refresh(self.cfg, profile, force=True),
            )
            reply = _render_worth(result, age_days(result))
        except Exception as exc:
            reply = f"⚠️ Не удалось получить данные о рынке: {exc}"
        await sent.edit_text(reply, parse_mode="HTML")

    async def handle_url_message(self, message) -> None:  # noqa: ANN001
        """Accept a pasted vacancy URL and run the SHARED add-by-URL flow.

        Same backend as the dashboard input (add_by_url.add_by_url): fetch ->
        SSRF guard -> insert (source_channel='manual') -> extract -> score ->
        advance(). The result is routed by score band in ``_deliver_by_band``.

        Feedback to the SENDER:
          - no URL in the text        -> ignored silently (e.g. /start, chatter).
          - invalid/unreadable URL    -> a short error reply, no card.
          - duplicate                 -> "уже в пайплайне (#id)", no second card.
          - added                     -> band routing delivers the card/line.

        The access_gate OUTER middleware already allowlist-gates this handler.
        """
        url = first_url(getattr(message, "text", None))
        if not url:
            return  # not a vacancy link; do not reply (avoids chatter spam)

        outcome = add_by_url_mod.add_by_url(self.conn, url, self.deps)

        if outcome.status == "invalid_url":
            await message.answer("это не похоже на ссылку на вакансию")
            return
        if outcome.status == "unreadable":
            await message.answer("не удалось прочитать страницу по ссылке")
            return
        if outcome.status == "duplicate":
            await message.answer(f"уже в пайплайне (#{outcome.item_id})")
            return

        # status == "added": deliver per score band, reusing existing renderers.
        await self._deliver_by_band(outcome.item_id)

    async def _deliver_by_band(self, item_id: int) -> None:
        """Route a freshly-added item to the operator by score band, REUSING the
        existing renderers (no parallel card renderer):

          - SURFACED (score >= 60): the full card + Approve/Backlog/Skip buttons
            via ``notify_surfaced`` — byte-identical to a harvested surfaced card.
          - borderline [50, 60): the COMPACT ``render_borderline`` block (the same
            renderer the /borderline list uses), no action buttons.
          - rejected (score < 50, incl. salary-guard hard rejects): a single
            "отклонено: score N · <reason>" line. The item sits in 'rejected'
            (visible in the dashboard) without spamming a full card.

        All three go to ``cfg.notify_chat_id`` — the same channel harvested cards
        use — so a surfaced add looks exactly like a surfaced harvest.
        """
        self._ensure()
        item = store.get_item(self.conn, item_id)
        if item is None:  # pragma: no cover - just-added item cannot vanish
            return

        # SURFACED -> the EXACT surfaced-card delivery harvested cards use.
        if item.state == SURFACED:
            await self.notify_surfaced(item_id)
            return

        score = item.relevance_score if item.relevance_score is not None else 0.0

        # Borderline band -> the compact /borderline renderer for a single item.
        if self.BORDERLINE_MIN <= score < self.BORDERLINE_MAX:
            await self._bot.send_message(
                self.cfg.notify_chat_id,
                render_borderline([item]),
                link_preview_options=_no_preview(),
            )
            return

        # Rejected -> a single processed-and-why line (int() truncates, never
        # rounds 49.x up to "50"). Reuses the borderline compact-reason helper.
        reason = _borderline_reason(item)
        line = f"отклонено: score {int(score)}"
        if reason:
            line += f" · {reason}"
        await self._bot.send_message(
            self.cfg.notify_chat_id,
            line,
            link_preview_options=_no_preview(),
        )

    async def notify_surfaced(self, item_id: int) -> None:
        """Send a surfaced job with approve/backlog/skip buttons."""
        self._ensure()
        item = store.get_item(self.conn, item_id)
        if item is None or item.state != SURFACED:
            return
        salary_rub = self._salary_rub(item)
        text = render_surfaced(item, salary_rub)
        kb = _build_keyboard(surfaced_keyboard_spec(item_id))
        await self._bot.send_message(
            self.cfg.notify_chat_id,
            text,
            reply_markup=kb,
            parse_mode="HTML",
            link_preview_options=_no_preview(),
        )

    async def notify_draft(self, item_id: int) -> None:
        """Send a generated draft to the operator for MANUAL sending.

        The отклик is a clean plain-text message the operator long-press-copies
        and sends themselves (DM / email / web form — no auto-send). The
        keyboard offers «✅ Отправила» (manual-confirm, advances DRAFTED->SENT)
        and, when an extracted contact fits, a «📋 Контакт» copy button.
        """
        self._ensure()
        item = store.get_item(self.conn, item_id)
        if item is None or item.state != DRAFTED:
            return
        text = render_draft(item)
        contact = self._contact_of(item)
        kb = _build_keyboard(draft_keyboard_spec(item_id, contact))
        # Draft text may contain a URL; disable the preview to keep it clean.
        await self._bot.send_message(
            self.cfg.notify_chat_id,
            text,
            reply_markup=kb,
            link_preview_options=_no_preview(),
        )

    @staticmethod
    def _contact_of(item: store.WorkItem) -> Optional[str]:
        """Extract the recruiter contact string from the item, or None. PURE-ish.

        Used to decide whether the «📋 Контакт» copy button is shown (the spec
        applies the <=256-char rule and the null-omit).
        """
        try:
            ex = from_dict(json.loads(item.extracted_json or "{}"))
        except Exception:
            return None
        return ex.contact

    def _salary_rub(self, item: store.WorkItem) -> Optional[float]:
        if self.deps.fx is None:
            return None
        try:
            ex = from_dict(json.loads(item.extracted_json or "{}"))
        except Exception:
            return None
        top = ex.salary_max if ex.salary_max is not None else ex.salary_min
        if top is None or ex.currency is None:
            return None
        try:
            return self.deps.fx.convert(top, ex.currency)
        except Exception:
            return None

    async def handle_callback(self, cb) -> str:  # noqa: ANN001
        """Process an inline-button press: validate, advance, ack. Returns the
        result status string (also used by tests)."""
        print(f"[callback] {cb.data!r}", flush=True)
        # Defense in depth: the outer middleware already gates updates, but
        # re-check here so the handler is safe even if invoked directly. Drop
        # silently (empty ack to stop the spinner) with NO business logic.
        if not is_allowed(_user_id_of(cb), self.cfg.allowed_user_ids):
            try:
                await cb.answer()
            except Exception:
                pass
            return "forbidden"
        decoded = decode_callback(cb.data or "")
        if decoded is None:
            await cb.answer("bad callback")
            return "bad_callback"
        action, item_id = decoded
        decision = ACTION_TO_DECISION.get(action)
        if decision is None:
            await cb.answer("unknown action")
            return "unknown_action"

        # Reconnect if the DB connection was killed between scheduled jobs.
        # Mirrors the reconnect guard in the scheduled jobs (serve.py), covering
        # the gap between daily harvests when the connection can die undetected.
        self.conn = await store.ensure_reconnected(self.conn, self.cfg.database_url)

        result = pipeline.advance_by_id(self.conn, item_id, decision=decision, deps=self.deps)

        # On approve, drive the LLM pipeline forward to a draft, then surface it.
        followup = None
        if result.status == "moved" and decision == DECISION_APPROVE:
            pipeline.run_to_gate(self.conn, item_id, deps=self.deps)
            item = store.get_item(self.conn, item_id)
            if item is not None and item.state == DRAFTED:
                followup = "drafted"

        ack = self._ack_text(decision, result.status)
        await cb.answer(ack)

        # Edit the card to a final-state line. When the new state still has
        # manual funnel actions (post-send: sent->screening->interview), SWAP the
        # keyboard for the next step instead of removing it. Otherwise (terminal
        # or the pre-send gates) REMOVE the keyboard so the card is no longer
        # actionable. For a no-op leave the text untouched but strip defensively.
        if result.status == "moved":
            next_spec = funnel_keyboard_spec(result.to_state, item_id)
            if next_spec:
                await self._restage_card(cb, decision, next_spec)
            else:
                await self._finalize_card(cb, decision)
        else:
            await self._strip_keyboard(cb)

        if followup == "drafted":
            await self.notify_draft(item_id)
        return result.status

    async def _finalize_card(self, cb, decision: str) -> None:  # noqa: ANN001
        """Append a final-state line to the card and drop the inline keyboard.

        Robust to a non-editable message (too old, deleted, identical text):
        falls back to merely stripping the keyboard, and never raises.
        """
        line = final_state_line(decision)
        message = getattr(cb, "message", None)
        if message is None:
            return
        original = getattr(message, "html_text", None) or getattr(message, "text", None) or ""
        new_text = f"{original}\n\n{line}" if line else original
        try:
            await message.edit_text(new_text, reply_markup=None, parse_mode="HTML")
            return
        except Exception:
            pass
        # Edit failed (e.g. message has no editable text / is too old). At least
        # remove the keyboard so the card is no longer actionable.
        await self._strip_keyboard(cb)

    async def _restage_card(self, cb, decision: str, spec) -> None:  # noqa: ANN001
        """Append the stage line and SWAP the keyboard to the next funnel step.

        Used after a move into a state that still has manual actions (the post-
        send funnel). Mirrors _finalize_card but sets a fresh keyboard instead of
        dropping it. Robust to a non-editable message: falls back to swapping
        only the keyboard, and never raises.
        """
        line = final_state_line(decision)
        kb = _build_keyboard(spec)
        message = getattr(cb, "message", None)
        if message is None:
            return
        original = getattr(message, "html_text", None) or getattr(message, "text", None) or ""
        new_text = f"{original}\n\n{line}" if line else original
        try:
            await message.edit_text(new_text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
        # Text edit failed: at least swap the keyboard so the next step is offered.
        try:
            await message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass

    @staticmethod
    async def _strip_keyboard(cb) -> None:  # noqa: ANN001
        """Remove the inline keyboard from the card, ignoring any edit error."""
        message = getattr(cb, "message", None)
        if message is None:
            return
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    @staticmethod
    def _ack_text(decision: str, status: str) -> str:
        if status != "moved":
            return f"no-op ({status})"
        return {
            DECISION_APPROVE: "Approved",
            DECISION_SKIP: "Skipped",
            DECISION_BACKLOG: "Backlogged",
            DECISION_SEND: "Sent",
            DECISION_SCREENING: "Скрининг",
            DECISION_INTERVIEW: "Собес",
            DECISION_OFFER: "Оффер 🎉",
            DECISION_DECLINE: "Отказ",
            DECISION_CLOSE: "Закрыто",
        }.get(decision, "Done")

    async def run(self) -> None:
        """Start long-polling. Blocks until cancelled."""
        self._ensure()
        await self._dp.start_polling(self._bot)

    # --- Session lifecycle --------------------------------------------------
    #
    # The aiogram Bot lazily opens an aiohttp ClientSession (and TCP/SSL
    # connector) on its FIRST HTTP request. That session MUST be closed
    # explicitly before the event loop is torn down; otherwise aiohttp emits
    # "Unclosed client session" / "Unclosed connector" and the loop closing
    # mid-flight surfaces SSL "Bad file descriptor" / "Event loop is closed".
    #
    # NOTE: we close ``bot.session`` (aiogram's AiohttpSession), NOT
    # ``Bot.close()`` -- the latter is the Telegram ``close`` API method that
    # logs the bot off the server. ``Bot.__aexit__`` does exactly
    # ``await self.session.close()``, which is what we want.

    async def aclose(self) -> None:
        """Close the underlying aiogram HTTP session exactly once.

        Idempotent and safe to call even if the Bot was never constructed
        (e.g. there were zero surfaced items, so ``_ensure`` never ran).
        """
        bot = self._bot
        if bot is None:
            return
        # Guard against double-close (the session itself is also idempotent,
        # but keep our own flag for clear ordering in tests/teardown).
        if getattr(self, "_closed", False):
            return
        self._closed = True
        await bot.session.close()

    async def __aenter__(self) -> "JobHunterBot":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()


async def notify(bot: "JobHunterBot", item_ids: List[int]) -> List[int]:
    """Send a surfaced card for EACH id, awaiting every send to completion.

    Sends are dispatched concurrently and then awaited together with
    ``asyncio.gather`` so this coroutine does not return until every HTTP
    request has actually finished -- no fire-and-forget, no un-awaited tasks.

    A failing send is isolated (``return_exceptions=True``) so one bad card
    does not abort the rest, but the gather still WAITS for every coroutine to
    settle before returning. Returns the list of ids that sent successfully.

    The session is NOT closed here -- the caller owns the Bot lifecycle and
    must close it (via ``async with bot`` or ``bot.aclose()``) only AFTER this
    coroutine returns, i.e. after all sends have completed.

    Pure of network in tests: pass a fake ``bot`` exposing
    ``notify_surfaced(item_id)`` as an awaitable.
    """
    import asyncio as _asyncio

    if not item_ids:
        return []
    coros = [bot.notify_surfaced(item_id) for item_id in item_ids]
    results = await _asyncio.gather(*coros, return_exceptions=True)
    sent: List[int] = []
    for item_id, res in zip(item_ids, results):
        if isinstance(res, Exception):
            print(f"[notify] item {item_id} send failed: {res}")
        else:
            sent.append(item_id)
    return sent


def build_deps(cfg: Config) -> Deps:
    """Construct live Deps (LLM client + FX) from config."""
    llm_client = None
    if cfg.anthropic_api_key:
        from .llm import AnthropicClient

        # The client default model is the cheap one; the judge model is passed
        # per-call by the pipeline via Deps.judge_model.
        llm_client = AnthropicClient(cfg.anthropic_api_key, cfg.cheap_model)
    fx = fx_mod.FxRates(provider=cfg.fx_provider, cache_ttl=cfg.fx_cache_ttl)
    # Load the candidate profile (local real profile if present, else the
    # generic committed example). It drives the rubric/draft prompts, the draft
    # signature and the salary floor.
    from .profile import load_profile

    profile = load_profile()
    return Deps(
        llm_client=llm_client, fx=fx,
        cheap_model=cfg.cheap_model, judge_model=cfg.judge_model,
        corridor_lo=cfg.score_corridor_lo, corridor_hi=cfg.score_corridor_hi,
        profile=profile,
    )


def main() -> None:
    """Long-running polling entrypoint (alias of ``python -m job_hunter.serve``).

    Delegates to ``job_hunter.serve.main`` so there is a SINGLE implementation of
    the startup -> polling -> graceful-teardown lifecycle (own DB connection in
    the polling thread, one asyncio.run, aclose + conn.close in a finally).
    """
    from .serve import main as serve_main

    serve_main()


if __name__ == "__main__":
    main()
