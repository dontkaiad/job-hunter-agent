"""Benefits analytics — benefit frequency derived from the collected vacancy pipeline.

Aggregates ``benefits`` lists from ``work_items.extracted_json`` for vacancies
with relevance_score >= 25 (the whole quality pool). The ``benefits`` field
stores canonical display labels (RU or EN, depending on post language) produced
by ``extract._detect_benefits``. This module maps them back to canonical keys
for consistent aggregation and exposes a reusable ``benefits_freq`` dict.

Entry point: ``compute_from_pipeline(conn, cfg)``

For tests: ``_aggregate_benefits(benefit_rows, ...)`` is pure (no DB).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Canonical benefit registry
# Maps canonical key -> (RU display label, EN display label).
# Mirrors extract._BENEFIT_LABELS exactly — kept in sync manually.
# RU label is used for display in the analytics block.
# ---------------------------------------------------------------------------
BENEFIT_REGISTRY: Dict[str, tuple] = {
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

# Reverse map: stored display label (RU or EN) -> canonical key.
_LABEL_TO_KEY: Dict[str, str] = {}
for _key, (_ru, _en) in BENEFIT_REGISTRY.items():
    _LABEL_TO_KEY[_ru.lower().strip()] = _key
    _LABEL_TO_KEY[_en.lower().strip()] = _key


def _label_to_canonical(label: str) -> Optional[str]:
    """Map a stored display label to its canonical key. None if unknown."""
    return _LABEL_TO_KEY.get(label.lower().strip())


def _display_name(canonical_key: str) -> str:
    """Return the RU display name for a canonical key."""
    entry = BENEFIT_REGISTRY.get(canonical_key)
    return entry[0] if entry else canonical_key


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class BenefitsAnalyticsResult:
    """Benefit frequency computed from the pipeline's quality vacancy pool."""

    top_benefits: List[Dict]
    """Sorted list of {benefit, count, pct} — top benefits by frequency."""

    benefits_freq: Dict[str, int]
    """Raw {canonical_key: count} mapping — for programmatic consumption."""

    total_pool: int
    """Vacancies with score >= 25 in scope (the full quality pool)."""

    vacancies_with_benefits: int
    """Of total_pool, how many had at least one benefit listed."""

    computed_at: str
    min_display_sample: int
    small_sample: bool
    """True when total_pool < min_display_sample."""

    degraded_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Pure aggregation (testable without DB)
# ---------------------------------------------------------------------------


def _aggregate_benefits(
    benefit_rows: List[List[str]],
    *,
    total_pool: int = 0,
    min_display_sample: int = 5,
) -> BenefitsAnalyticsResult:
    """Pure function: aggregate a list of benefit label lists.

    benefit_rows: each element is the ``benefits`` list from one vacancy's
    extracted_json. Unknown/unrecognised labels are silently skipped.
    Each benefit is counted at most once per vacancy.
    """
    from .clock import now_iso

    freq: Dict[str, int] = {}
    vacancies_with_benefits = 0

    for labels in benefit_rows:
        if not labels:
            continue
        seen_in_vacancy: set = set()
        contributed = False
        for label in labels:
            if not isinstance(label, str) or not label.strip():
                continue
            key = _label_to_canonical(label)
            if key is None or key in seen_in_vacancy:
                continue
            seen_in_vacancy.add(key)
            freq[key] = freq.get(key, 0) + 1
            contributed = True
        if contributed:
            vacancies_with_benefits += 1

    sorted_freq = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    base = vacancies_with_benefits or 1
    top_benefits = [
        {
            "benefit": _display_name(k),
            "canonical": k,
            "count": c,
            "pct": round(c / base * 100, 1),
        }
        for k, c in sorted_freq
    ]
    benefits_freq = {k: c for k, c in sorted_freq}

    small_sample = total_pool < min_display_sample
    degraded_reason = (
        f"Выборка мала ({total_pool} вакансий в пуле, нужно ≥{min_display_sample})"
        if small_sample
        else None
    )

    return BenefitsAnalyticsResult(
        top_benefits=top_benefits,
        benefits_freq=benefits_freq,
        total_pool=total_pool,
        vacancies_with_benefits=vacancies_with_benefits,
        computed_at=now_iso(),
        min_display_sample=min_display_sample,
        small_sample=small_sample,
        degraded_reason=degraded_reason,
    )


# ---------------------------------------------------------------------------
# DB computation
# ---------------------------------------------------------------------------


def compute_from_pipeline(conn, cfg) -> BenefitsAnalyticsResult:
    """Query work_items with score >= 25 and aggregate benefit frequencies.

    Pure-SQL fetch + Python aggregation — zero API calls.
    """
    min_display_sample = getattr(cfg, "stack_min_sample", 5)

    rows = conn.execute(
        """
        SELECT extracted_json
        FROM work_items
        WHERE relevance_score >= 25
          AND extracted_json IS NOT NULL
        """,
    ).fetchall()

    total_pool = len(rows)
    benefit_rows: List[List[str]] = []

    for row in rows:
        try:
            e = json.loads(row["extracted_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        benefits = e.get("benefits")
        benefit_rows.append(benefits if isinstance(benefits, list) else [])

    return _aggregate_benefits(
        benefit_rows,
        total_pool=total_pool,
        min_display_sample=min_display_sample,
    )
