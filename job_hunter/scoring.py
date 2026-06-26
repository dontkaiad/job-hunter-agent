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
    stripped = probe.strip()

    if not stripped:
        return PrefilterResult(False, "empty/no text")

    # Strip whitespace to count real characters (a wall of newlines is junk).
    meaningful = re.sub(r"\s+", "", stripped)
    if len(meaningful) < _MIN_MEANINGFUL_CHARS:
        return PrefilterResult(False, "too short to be a vacancy")

    if _NON_JOB_RE.search(stripped):
        return PrefilterResult(False, "looks like a candidate 'looking for work' post")

    return PrefilterResult(True, None)


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
