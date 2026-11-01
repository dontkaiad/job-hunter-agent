"""PURE: the reason chips offered when a card is rejected at the entry gate («Отклонить»
in the dashboard; the state is still ``skipped``) and how they are analysed.

Why a skip happened is what makes the skip a usable label for the scoring eval:
the operator skips a vacancy for reasons the scorer cannot or should not know
(it is stale, the candidate would not be hired there), and only some skips are
a verdict on role fit.

Several chips may be set on one vacancy. Stored as sorted, comma-joined codes.
"""
from __future__ import annotations

from typing import Dict, Iterable, List

# code -> button label (fixed order = display order)
CHIPS: Dict[str, str] = {
    "stale": "🕰 неактуальна/старая",
    "geo": "🌍 не возьмут: гео/виза/резидентство",
    "fit": "🧩 не мой профиль/стек",
    "seniority": "📶 seniority не тот",
    "pay": "💰 зарплата",
    "other": "… другое",
}

# How the eval treats each chip (decided with the operator, 2026-11-01):
#  * a skip counts as a NEGATIVE relevance label only with fit and/or seniority;
#  * stale is excluded from precision (the scorer cannot know freshness);
#  * geo is analysed separately: it is a signal for the hard guards, not for scoring.
RELEVANCE_NEGATIVE = frozenset({"fit", "seniority"})
EXCLUDED_FROM_PRECISION = frozenset({"stale"})
GUARD_SIGNAL = frozenset({"geo"})


def normalize(codes: Iterable[str]) -> List[str]:
    """Known codes only, de-duplicated, in display order. PURE."""
    want = set(codes)
    return [c for c in CHIPS if c in want]


def encode(codes: Iterable[str]) -> str:
    return ",".join(normalize(codes))


def decode(value: str) -> List[str]:
    return normalize(c for c in (value or "").split(",") if c)


def toggle(current: Iterable[str], code: str) -> List[str]:
    """Add ``code`` if absent, remove if present. Unknown codes are ignored. PURE."""
    cur = set(normalize(current))
    if code not in CHIPS:
        return normalize(cur)
    cur ^= {code}
    return normalize(cur)
