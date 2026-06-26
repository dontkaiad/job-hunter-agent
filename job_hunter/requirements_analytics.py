"""Requirements analytics — seniority, work format, and relocation data from the pipeline.

Aggregates ``seniority``, ``remote``, ``location``, and ``relocation`` fields
from ``work_items.extracted_json`` for vacancies with relevance_score >= 25.

Hybrid detection: extract.py encodes hybrid as ``remote=True`` + ``"(гибрид)"``
suffix in ``location``, so we can split remote→4 categories:
  "remote"  — удалёнка (remote=True, no hybrid tag)
  "hybrid"  — гибрид   (remote=True, "(гибрид)" in location)
  "office"  — офис     (remote=False)
  "unknown" — не указано (remote=None)

Entry point: ``compute_from_pipeline(conn, cfg)``

For tests: ``_aggregate_requirements(rows, ...)`` is pure (no DB).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

HYBRID_TAG = "(гибрид)"

# Canonical grade display order (natural progression for display)
GRADE_ORDER = ["junior", "middle", "middle+", "senior", "lead"]

# Remote category labels (key → display)
REMOTE_LABELS: Dict[str, str] = {
    "remote": "Удалёнка",
    "hybrid": "Гибрид",
    "office": "Офис",
    "unknown": "Не указано",
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class RequirementsAnalyticsResult:
    """Seniority + work-format + relocation aggregated from the quality pool."""

    seniority_freq: Dict[str, int]
    """Raw {canonical_grade: count} — for programmatic consumption (Competencies)."""

    seniority_dist: List[Dict]
    """[{grade, count, pct}, ...] sorted by GRADE_ORDER then by count desc."""

    remote_dist: List[Dict]
    """[{label, key, count, pct}, ...] for remote/hybrid/office/unknown."""

    relocation_count: int
    """Vacancies with relocation=True."""

    relocation_pct: float
    """relocation_count / total_pool * 100, rounded to 1 dp."""

    total_pool: int
    """Vacancies with score >= 25 (full quality pool)."""

    vacancies_with_seniority: int
    """Of total_pool, how many had a non-None seniority."""

    computed_at: str
    min_display_sample: int
    small_sample: bool
    degraded_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Pure aggregation (testable without DB)
# ---------------------------------------------------------------------------


def _remote_key(remote: Optional[bool], location: Optional[str]) -> str:
    """Map (remote, location) pair to one of the 4 canonical remote keys."""
    if remote is True:
        if location and HYBRID_TAG in location:
            return "hybrid"
        return "remote"
    if remote is False:
        return "office"
    return "unknown"


def _aggregate_requirements(
    rows: List[Dict],
    *,
    total_pool: int = 0,
    min_display_sample: int = 5,
) -> RequirementsAnalyticsResult:
    """Pure function: aggregate requirement fields from a list of extracted_json dicts.

    Each row is expected to have keys: seniority, remote, location, relocation.
    Missing / None values are handled gracefully.
    """
    from .clock import now_iso

    seniority_freq: Dict[str, int] = {}
    remote_counts: Dict[str, int] = {k: 0 for k in REMOTE_LABELS}
    relocation_count = 0
    vacancies_with_seniority = 0

    for row in rows:
        # Seniority
        grade = row.get("seniority")
        if grade and isinstance(grade, str):
            grade = grade.strip().lower()
            if grade:
                seniority_freq[grade] = seniority_freq.get(grade, 0) + 1
                vacancies_with_seniority += 1

        # Remote / hybrid / office / unknown
        remote = row.get("remote")
        location = row.get("location") or ""
        rkey = _remote_key(remote, location)
        remote_counts[rkey] += 1

        # Relocation
        if row.get("relocation") is True:
            relocation_count += 1

    # Seniority dist: GRADE_ORDER first, then unknowns, sorted by count within each tier
    base_s = vacancies_with_seniority or 1
    known_grades = [g for g in GRADE_ORDER if g in seniority_freq]
    other_grades = sorted(
        (g for g in seniority_freq if g not in GRADE_ORDER),
        key=lambda g: seniority_freq[g],
        reverse=True,
    )
    seniority_dist = [
        {
            "grade": g,
            "count": seniority_freq[g],
            "pct": round(seniority_freq[g] / base_s * 100, 1),
        }
        for g in known_grades + other_grades
    ]

    # Remote dist: sort by count desc
    base_r = total_pool or 1
    remote_dist = sorted(
        [
            {
                "key": k,
                "label": REMOTE_LABELS[k],
                "count": c,
                "pct": round(c / base_r * 100, 1),
            }
            for k, c in remote_counts.items()
        ],
        key=lambda x: x["count"],
        reverse=True,
    )

    relocation_pct = round(relocation_count / (total_pool or 1) * 100, 1)

    small_sample = total_pool < min_display_sample
    degraded_reason = (
        f"Выборка мала ({total_pool} вакансий в пуле, нужно ≥{min_display_sample})"
        if small_sample
        else None
    )

    return RequirementsAnalyticsResult(
        seniority_freq=seniority_freq,
        seniority_dist=seniority_dist,
        remote_dist=remote_dist,
        relocation_count=relocation_count,
        relocation_pct=relocation_pct,
        total_pool=total_pool,
        vacancies_with_seniority=vacancies_with_seniority,
        computed_at=now_iso(),
        min_display_sample=min_display_sample,
        small_sample=small_sample,
        degraded_reason=degraded_reason,
    )


# ---------------------------------------------------------------------------
# DB computation
# ---------------------------------------------------------------------------


def compute_from_pipeline(conn, cfg) -> RequirementsAnalyticsResult:
    """Query work_items with score >= 25 and aggregate requirement fields.

    Pure-SQL fetch + Python aggregation — zero API calls.
    """
    min_display_sample = getattr(cfg, "stack_min_sample", 5)

    db_rows = conn.execute(
        """
        SELECT extracted_json
        FROM work_items
        WHERE relevance_score >= 25
          AND extracted_json IS NOT NULL
        """,
    ).fetchall()

    total_pool = len(db_rows)
    rows: List[Dict] = []

    for row in db_rows:
        try:
            e = json.loads(row["extracted_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        rows.append({
            "seniority": e.get("seniority"),
            "remote": e.get("remote"),
            "location": e.get("location"),
            "relocation": e.get("relocation"),
        })

    return _aggregate_requirements(
        rows,
        total_pool=total_pool,
        min_display_sample=min_display_sample,
    )
