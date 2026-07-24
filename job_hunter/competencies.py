"""Competencies: Kai's hand-curated skill inventory + market-demand sync.

Content resolution mirrors ``job_hunter.profile`` exactly, because it is the
SAME kind of data (real, personal, not fit for the public repo):
  1. ``config/competencies.local.yaml`` — the REAL inventory (gitignored).
  2. ``config/competencies.example.yaml`` — a generic, fictional placeholder
     (committed; what the public repo and the test suite see).

Filling the local file with REAL content is a deliberately separate, manual
pass (per project: job-hunter, Nexus, Arcana, klgpff, mox-bunfarm) done by
reading each project's code. This module only defines the SHAPE and the sync
logic; it does not itself decide what belongs in any bucket.

Shape is TERM-FIRST, not project-first: one entry per skill/technique, with a
list of ``Evidence`` (project + file/function pointer) nested inside. The
same term found in two projects is ONE entry with two evidence rows, not two
separate entries — a project-first shape would duplicate every base skill
(Python, PostgreSQL, ...) once per project and get unreadable fast as more
projects are reviewed. Every core/growing entry needs at least one evidence
row; an entry with none is content still awaiting the code-reading pass, not
something to take on faith. "skip" entries are the one exception (see BUCKETS
below) — they carry no evidence by design.

What IS code here, not hand-typed content: matching those claims against real
market demand. ``sync_with_market`` crosses the loaded entries against
``stack_analytics`` tech-frequency counts (which are themselves derived from
the extract pipeline's ``stack`` field on every harvested vacancy — see
stack_analytics.py), so the "does the market actually ask for this" side of
the picture is always live, never hand-maintained.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PKG_DIR)
_CONFIG_DIR = os.path.join(_REPO_ROOT, "config")

LOCAL_COMPETENCIES_PATH = os.path.join(_CONFIG_DIR, "competencies.local.yaml")
EXAMPLE_COMPETENCIES_PATH = os.path.join(_CONFIG_DIR, "competencies.example.yaml")

# The 4 buckets from the spec: сильное ядро / в развитии / сознательно не
# качаю / словарь. Order matters — it's the display order in the UI.
BUCKETS = ("core", "growing", "skip", "glossary")


@dataclass(frozen=True)
class Evidence:
    """One (project, code pointer) proof for a competency entry.

    ``source_ref`` is the SOURCE OF TRUTH pointer — "path/to/file.py:LINE
    (symbol)" — checked against the actual repo, not recalled from memory.
    ``note`` is an optional short, project-specific detail (what the term is
    USED FOR in that project, if it's not obvious from source_ref alone).
    """

    project: str
    source_ref: str
    note: str = ""


@dataclass(frozen=True)
class CompetencyEntry:
    """One skill/term, with evidence from however many projects prove it.

    ``resume_line`` is only meaningful for bucket="core".
    """

    bucket: str
    term_ru: str
    term_en: str
    explainer_ru: str = ""
    explainer_en: str = ""
    resume_line: str = ""
    evidence: Tuple[Evidence, ...] = field(default_factory=tuple)


def _as_str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _parse_evidence(raw: Any) -> Tuple[Evidence, ...]:
    if not isinstance(raw, list):
        return ()
    out: List[Evidence] = []
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        project = _as_str(row.get("project"))
        source_ref = _as_str(row.get("source_ref"))
        if not project and not source_ref:
            continue
        out.append(Evidence(project=project, source_ref=source_ref, note=_as_str(row.get("note"))))
    return tuple(out)


def parse_competencies(
    data: Any, source_path: Optional[str] = None
) -> List[CompetencyEntry]:
    """Turn an already-loaded YAML mapping into entries. PURE.

    Expected shape: {bucket: [ {term_ru, term_en, ..., evidence: [...]}, ... ]}
    keyed by the BUCKETS names. Unknown top-level keys and rows missing BOTH
    term_ru and term_en are skipped — best-effort, content review happens by
    reading the file, not by failing the whole load over one bad row.
    """
    if not isinstance(data, Mapping):
        return []
    out: List[CompetencyEntry] = []
    for bucket in BUCKETS:
        rows = data.get(bucket)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            term_en = _as_str(row.get("term_en"))
            term_ru = _as_str(row.get("term_ru"))
            if not term_en and not term_ru:
                continue
            out.append(
                CompetencyEntry(
                    bucket=bucket,
                    term_ru=term_ru,
                    term_en=term_en,
                    explainer_ru=_as_str(row.get("explainer_ru")),
                    explainer_en=_as_str(row.get("explainer_en")),
                    resume_line=_as_str(row.get("resume_line")),
                    evidence=_parse_evidence(row.get("evidence")),
                )
            )
    return out


def load_competencies_file(path: str) -> List[CompetencyEntry]:
    """Read and parse a single YAML competencies file. I/O."""
    import yaml

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return parse_competencies(data, source_path=path)


def resolve_competencies_path(
    local_path: str = LOCAL_COMPETENCIES_PATH,
    example_path: str = EXAMPLE_COMPETENCIES_PATH,
) -> str:
    """Return the local (real) path if present, else the example. PURE-ish."""
    if os.path.exists(local_path):
        return local_path
    return example_path


def load_competencies(
    local_path: str = LOCAL_COMPETENCIES_PATH,
    example_path: str = EXAMPLE_COMPETENCIES_PATH,
) -> List[CompetencyEntry]:
    """Load the competencies inventory: local (real) if present, else example."""
    return load_competencies_file(resolve_competencies_path(local_path, example_path))


# ---------------------------------------------------------------------------
# Market sync — PURE, no I/O.
# ---------------------------------------------------------------------------


def _match_key(text: str) -> str:
    return text.lower().strip()


def sync_with_market(
    entries: List[CompetencyEntry],
    tech_freq: Dict[str, int],
    vacancies_with_stack: int,
) -> Dict[str, Any]:
    """Cross competency entries against stack_analytics tech frequency. PURE.

    Matching is an EXACT case-insensitive match on ``term_en`` (falling back
    to ``term_ru`` only when term_en is empty) against the ALREADY-CANONICAL
    display names stack_analytics produces (e.g. "PyTorch", "LLM"). This only
    gives a signal for entries phrased as market-recognizable keywords
    ("Python", "PostgreSQL", "Docker") — narrative/architecture entries
    ("cost-aware LLM routing") are expected to sit at 0%, that's not a bug,
    market_stack terms are single recognizable tech names, not sentences.

    Returns:
      matched — one row per input entry (dict of its fields, including its
        evidence list, + market_count/market_pct), in input order.
      gap_candidates — market terms with count > 0 that NO entry (in any
        bucket) matches, sorted by count desc: candidates for a new entry.
    """
    base = vacancies_with_stack or 1
    freq_by_key = {_match_key(k): (k, v) for k, v in tech_freq.items()}

    matched_keys: set = set()
    matched: List[Dict[str, Any]] = []
    for e in entries:
        key = _match_key(e.term_en) if e.term_en else _match_key(e.term_ru)
        count = 0
        if key and key in freq_by_key:
            _, count = freq_by_key[key]
            matched_keys.add(key)
        matched.append(
            {
                "bucket": e.bucket,
                "term_ru": e.term_ru,
                "term_en": e.term_en,
                "explainer_ru": e.explainer_ru,
                "explainer_en": e.explainer_en,
                "resume_line": e.resume_line,
                "evidence": [
                    {"project": ev.project, "source_ref": ev.source_ref, "note": ev.note}
                    for ev in e.evidence
                ],
                "market_count": count,
                "market_pct": round(count / base * 100, 1) if count else 0.0,
            }
        )

    gap_candidates = [
        {
            "term": display,
            "market_count": count,
            "market_pct": round(count / base * 100, 1),
        }
        for key, (display, count) in freq_by_key.items()
        if key not in matched_keys
    ]
    gap_candidates.sort(key=lambda x: x["market_count"], reverse=True)

    return {"matched": matched, "gap_candidates": gap_candidates}
