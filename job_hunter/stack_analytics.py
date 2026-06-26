"""Stack analytics — tech frequency derived from the collected vacancy pipeline.

Aggregates ``stack`` lists from ``work_items.extracted_json`` for vacancies
with relevance_score >= 25 (the whole quality pool). Returns tech frequency
counts usable both for display and programmatic consumption by future modules
(e.g. Competencies sync).

Entry point: ``compute_from_pipeline(conn, cfg)``

For tests: ``_aggregate_stack(stack_rows, min_display_sample)`` is pure (no DB).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Canonical display names — normalize key → preferred casing for display.
# Key = lowercased tech string, value = canonical display label.
# Add entries when LLM produces inconsistent casing across vacancies.
# ---------------------------------------------------------------------------
_CANONICAL: Dict[str, str] = {
    "python": "Python",
    "pytorch": "PyTorch",
    "tensorflow": "TensorFlow",
    "langchain": "LangChain",
    "langgraph": "LangGraph",
    "llamaindex": "LlamaIndex",
    "openai": "OpenAI",
    "fastapi": "FastAPI",
    "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL",
    "clickhouse": "ClickHouse",
    "elasticsearch": "Elasticsearch",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "airflow": "Airflow",
    "dbt": "dbt",
    "mlflow": "MLflow",
    "huggingface": "HuggingFace",
    "qdrant": "Qdrant",
    "weaviate": "Weaviate",
    "pinecone": "Pinecone",
    "chromadb": "ChromaDB",
    "redis": "Redis",
    "kafka": "Kafka",
    "aws": "AWS",
    "gcp": "GCP",
    "azure": "Azure",
    "git": "Git",
    "github": "GitHub",
    "gitlab": "GitLab",
    "llm": "LLM",
    "rag": "RAG",
    "nlp": "NLP",
    "sql": "SQL",
    "pydantic": "Pydantic",
    "celery": "Celery",
    "nginx": "Nginx",
    "linux": "Linux",
    "bash": "Bash",
}


def _normalize_key(tech: str) -> str:
    """Return a deduplicated lookup key (lowercase, stripped)."""
    return tech.lower().strip()


def _display_name(tech: str) -> str:
    """Return canonical display name for a tech string."""
    key = _normalize_key(tech)
    return _CANONICAL.get(key, tech.strip())


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class StackAnalyticsResult:
    """Tech frequency computed from the pipeline's quality vacancy pool."""

    top_tech: List[Dict]
    """Sorted list of {tech, count, pct} — top technologies by frequency."""

    tech_freq: Dict[str, int]
    """Raw {canonical_tech: count} mapping — for programmatic consumption."""

    total_pool: int
    """Vacancies with score >= 25 and state in scope (the full quality pool)."""

    vacancies_with_stack: int
    """Of total_pool, how many had a non-empty stack list."""

    computed_at: str
    min_display_sample: int
    small_sample: bool
    """True when total_pool < min_display_sample — data is noisy, warn user."""

    degraded_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Pure aggregation (testable without DB)
# ---------------------------------------------------------------------------


def _aggregate_stack(
    stack_rows: List[List[str]],
    *,
    total_pool: int = 0,
    min_display_sample: int = 5,
    top_n: int = 20,
) -> StackAnalyticsResult:
    """Pure function: aggregate a list of stack lists into StackAnalyticsResult.

    stack_rows: each element is the ``stack`` list from one vacancy's extracted_json.
    Empty or None lists are counted as vacancies_without_stack (excluded from freq).
    """
    from .clock import now_iso

    freq: Dict[str, int] = {}
    vacancies_with_stack = 0

    for stack in stack_rows:
        if not stack:
            continue
        vacancies_with_stack += 1
        seen_in_vacancy: set = set()
        for tech in stack:
            if not isinstance(tech, str) or not tech.strip():
                continue
            key = _normalize_key(tech)
            if key in seen_in_vacancy:
                continue
            seen_in_vacancy.add(key)
            display = _display_name(tech)
            freq[display] = freq.get(display, 0) + 1

    sorted_freq = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    base = vacancies_with_stack or 1  # avoid division by zero
    top_tech = [
        {"tech": t, "count": c, "pct": round(c / base * 100, 1)}
        for t, c in sorted_freq[:top_n]
    ]
    tech_freq = {t: c for t, c in sorted_freq}

    small_sample = total_pool < min_display_sample
    degraded_reason = (
        f"Выборка мала ({total_pool} вакансий в пуле, нужно ≥{min_display_sample})"
        if small_sample
        else None
    )

    return StackAnalyticsResult(
        top_tech=top_tech,
        tech_freq=tech_freq,
        total_pool=total_pool,
        vacancies_with_stack=vacancies_with_stack,
        computed_at=now_iso(),
        min_display_sample=min_display_sample,
        small_sample=small_sample,
        degraded_reason=degraded_reason,
    )


# ---------------------------------------------------------------------------
# DB computation
# ---------------------------------------------------------------------------


def compute_from_pipeline(conn, cfg) -> StackAnalyticsResult:
    """Query work_items with score >= 25 and aggregate stack frequencies.

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
    stack_rows: List[List[str]] = []

    for row in rows:
        try:
            e = json.loads(row["extracted_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        stack = e.get("stack")
        stack_rows.append(stack if isinstance(stack, list) else [])

    return _aggregate_stack(
        stack_rows,
        total_pool=total_pool,
        min_display_sample=min_display_sample,
    )
