"""PURE: lenient prefilter + score clamp/threshold + deterministic salary guard.

No I/O, no network, no clock. This module no longer computes a hand-tuned
weighted score: the relevance_score is produced by the Sonnet rubric judge
(see llm.py ``llm_score`` invoked from pipeline.py). What stays PURE here:

  1. ``prefilter`` — a LENIENT deterministic gate that drops ONLY obvious
     non-jobs (empty/junk text, clearly-not-a-vacancy). High recall: when in
     doubt, KEEP. This is NOT the relevance scorer.
  2. ``clamp_score`` — validate/clamp the LLM score into 0..100 (int).
  3. ``passes_threshold`` — deterministic surfaced-vs-rejected cut at T applied
     to the (already clamped) LLM score.
  4. ``salary_guard_reject`` — the HARD deterministic salary floor: regardless
     of the LLM score, if the salary top is known and below the candidate's
     EUR/month-gross floor (from the loaded profile) -> reject. Both the salary
     top AND the EUR floor are converted to a COMMON currency (RUB) by the I/O
     layer (fx.py) upstream; this function receives the two already-converted
     RUB values and only compares them, so the floor works regardless of the
     posting's currency.
  5. ``location_guard_reject`` — the HARD deterministic location/visa floor:
     regardless of the LLM score, an office-only vacancy with no relocation/visa
     support is physically unavailable to the candidate (no right to work there
     without sponsorship) -> reject, no matter how well the stack matches.

Scale: relevance_score in 0..100. Surface threshold T = 60.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .schema_extract import ExtractResult

# --- Tunables ---------------------------------------------------------------

SURFACE_THRESHOLD = 60          # T: at/above -> surfaced, below -> rejected

# Confidence corridor: Haiku scores in [SCORE_CORRIDOR_LO, SCORE_CORRIDOR_HI]
# trigger a full judge re-score (score becomes final from judge model).
# Intentionally wider than the threshold on both sides — catches vacancies Haiku
# might mis-score in either direction near the decision boundary.
# Overridable via SCORE_CORRIDOR_LO / SCORE_CORRIDOR_HI env vars (see config.py).
SCORE_CORRIDOR_LO = 50
SCORE_CORRIDOR_HI = 70

# Hard salary floor (EUR / month GROSS equivalent). The ACTUAL floor is loaded
# from the candidate profile (``Profile.salary_floor_eur`` in
# config/profile.*.yaml) and flows through the pipeline to ``salary_guard_reject``
# as ``floor_rub``; it is NOT hardcoded here. This module-level value is only a
# GENERIC fallback default used when no profile floor is supplied. The comparison
# is done in a common currency (RUB) so it holds for any posting currency: the
# I/O layer (fx.py) converts BOTH the posting's salary top AND the EUR floor to
# RUB via the live rate table, and ``salary_guard_reject`` compares the two RUB
# numbers. Salary fields (salary_min/max) are interpreted as MONTHLY.
DEFAULT_SALARY_FLOOR_EUR = 1000.0

# Back-compat alias (callers/tests historically referenced this name). It is the
# GENERIC default, NOT a real candidate figure — the live floor comes from the
# profile.
MIN_SALARY_EUR_PER_MONTH = DEFAULT_SALARY_FLOOR_EUR

# Minimum amount of "real" text for a post to even look like a vacancy. Below
# this the post is almost certainly junk (a sticker caption, a one-word ping).
_MIN_MEANINGFUL_CHARS = 25

# Signals that a post is clearly NOT a vacancy. Kept deliberately SMALL so the
# prefilter stays lenient (high recall). Only unambiguous non-jobs are dropped.
_NON_JOB_PATTERNS = [
    r"\bищу работу\b",            # candidate looking for work (not a vacancy)
    r"\bлищу работу\b",
    r"\bresume\b.*\bлищу\b",
    r"\bищу вакансию\b",
    r"#резюме\b",
    r"#resume\b",
    r"\bopen to work\b",
]
_NON_JOB_RE = re.compile("|".join(_NON_JOB_PATTERNS), re.I)


@dataclass
class PrefilterResult:
    keep: bool
    reason: Optional[str] = None  # why dropped (None when kept)


def text_prefilter(raw_text: Optional[str]) -> PrefilterResult:
    """LENIENT deterministic gate over RAW TEXT ALONE. PURE, no ExtractResult.

    The T1 entrypoint: runs BEFORE the paid extract LLM call (pipeline.py
    ``_do_extract``), so it can only see ``raw_text`` — there is no
    ExtractResult yet. Same rules ``prefilter`` applies post-extract; factored
    out so both callers share one set of thresholds/patterns.
    """
    stripped = (raw_text or "").strip()

    if not stripped:
        return PrefilterResult(False, "empty/no text")

    # Strip whitespace to count real characters (a wall of newlines is junk).
    meaningful = re.sub(r"\s+", "", stripped)
    if len(meaningful) < _MIN_MEANINGFUL_CHARS:
        return PrefilterResult(False, "too short to be a vacancy")

    if _NON_JOB_RE.search(stripped):
        return PrefilterResult(False, "looks like a candidate 'looking for work' post")

    return PrefilterResult(True, None)


# AI/ML/LLM topic signal for the T1 topic gate (see ``topic_prefilter``).
# Small and deliberately generic — this is a RECALL-first keyword list, not a
# classifier: it only needs to catch obvious off-topic noise (frontend-only,
# plain DevOps, etc.), not exhaustively describe the AI field. Case-insensitive
# substring match against title + a short prefix of the body.
_AI_TOPIC_KEYWORDS = [
    "ai", "ml", "llm", "gpt", "genai", "gen ai",
    "machine learning", "artificial intelligence", "deep learning",
    "nlp", "rag", "prompt", "prompt engineering", "data science",
    "data scientist", "computer vision", "neural network",
    "искусственный интеллект", "машинное обучение", "нейросет",
]
# Length of the raw-text prefix probed (title is checked separately, in full).
_TOPIC_PROBE_CHARS = 500
# \b-wrapped: short acronyms like "ai"/"ml"/"rag" are common substrings of
# ordinary words ("email", "html", "average") and would false-positive
# without word boundaries.
_AI_TOPIC_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in _AI_TOPIC_KEYWORDS) + r")\b", re.I
)


def topic_prefilter(raw_text: Optional[str], title: Optional[str] = None) -> PrefilterResult:
    """Cheap AI/ML/LLM topic signal over RAW TEXT (title + a short prefix).

    NOT a relevance judge — it is a recall-first keyword gate meant to catch
    only the OBVIOUS off-topic case (e.g. a frontend-only or plain-DevOps
    posting with zero AI/ML/LLM vocabulary anywhere near the top). A legit
    AI-team posting titled "Backend Engineer" that mentions the stack further
    down, or uses vocabulary not in the list, is expected to still pass —
    false negatives here are a cost problem (one skipped Haiku call), false
    positives are a correctness problem (a real fit silently dropped), so this
    stays lenient by design. See pipeline._do_extract for how the caller
    currently treats a miss (dry-run log vs. real drop, per Deps.topic_gate_enforce).
    """
    text = raw_text or ""
    probe = ((title or "") + " " + text[:_TOPIC_PROBE_CHARS]).strip()
    if not probe:
        return PrefilterResult(True, None)
    if _AI_TOPIC_RE.search(probe):
        return PrefilterResult(True, None)
    return PrefilterResult(False, "no AI/ML/LLM keyword in title/prefix")


def prefilter(extracted: ExtractResult, raw_text: Optional[str] = None) -> PrefilterResult:
    """LENIENT deterministic gate. Returns keep=True for anything plausible.

    Drops ONLY obvious non-jobs:
      - empty / whitespace-only / too-short text (junk, stickers, pings),
      - posts that are clearly a candidate's own "looking for work" notice.

    Everything else is KEPT for the Sonnet relevance judge. This is NOT the
    relevance scorer — borderline jobs MUST survive here.
    """
    text = (raw_text if raw_text is not None else "") or ""
    # Fall back to the title when no raw text is supplied (keeps it usable from
    # places that only have the structured result).
    probe = text if text.strip() else (extracted.title or "")
    return text_prefilter(probe)


def clamp_score(value) -> int:
    """Validate + clamp a model score into an int in [0, 100]. PURE.

    Non-numeric / None -> 0 (treated as the safest low score). The Sonnet parser
    in llm.py already coerces; this is a defensive second clamp at the scoring
    boundary so a bad value can never escape into the threshold logic.
    """
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, n))


def passes_threshold(score_value) -> bool:
    """T4 vs T3: at/above T -> surfaced, below -> rejected. PURE."""
    return clamp_score(score_value) >= SURFACE_THRESHOLD


def salary_guard_reject(
    salary_max_rub: Optional[float],
    floor_rub: Optional[float],
) -> bool:
    """Deterministic HARD salary floor (SCORING.md). PURE.

    Compares two amounts ALREADY converted to a common currency (RUB) by the
    I/O layer (fx.py): the KNOWN top of the posting's salary range and the
    candidate's EUR/month-gross floor (from the profile). Returns True (=>
    reject) ONLY when the salary top is known and strictly below the floor.
    Independent of the LLM score — it overrides it.

    Unknown salary (``salary_max_rub`` is None) is NOT a reject. If the floor
    could not be converted (``floor_rub`` is None, e.g. FX unavailable) the
    guard cannot fire and returns False — better to surface for a human than to
    reject on a missing rate.
    """
    if salary_max_rub is None or floor_rub is None:
        return False
    return salary_max_rub < floor_rub


def location_guard_reject(remote: Optional[bool], relocation: Optional[bool]) -> bool:
    """Deterministic HARD location/visa floor. PURE.

    An office/offline vacancy with no relocation or visa-sponsorship signal is
    physically unavailable to the candidate — there is no right to work in
    that country without sponsorship — regardless of how well the stack
    matches. Independent of the LLM score — it overrides it, same as
    ``salary_guard_reject``.

    Triggers only when ``remote`` is CONFIRMED False: both the heuristic
    extractor (extract.py) and the LLM extract prompt only set remote=False
    on an explicit office/on-site signal, never as a default ("remote = null
    only when NOTHING indicates a work format") — so False here is never a
    stand-in for "unknown". Unknown remote (``None``) does NOT trigger the
    guard.

    ``relocation`` is treated as absent unless explicitly True. Its "no
    signal" state in this codebase is ``None`` (the heuristic extractor only
    ever returns True or None; the LLM prompt has no equivalent "false only
    when explicitly ruled out" instruction for this field the way it does for
    remote) — so a typical office post that simply never mentions
    relocation/visa support extracts as ``relocation=None``, and that MUST
    still trip the guard, or it would only ever fire on the rare post that
    explicitly says "no relocation".
    """
    return remote is False and relocation is not True
