"""PURE: digest detection + splitting for multi-vacancy bundle posts.

Some channels (getmatch-style) pack MANY vacancies into a single message. If
such a post is extracted as one work_item the fields get contaminated (one
title, one salary, one contact mixing several jobs). This module provides pure
helpers to:

  - detect whether a raw_text is a digest (several vacancies in one message),
  - split it into per-vacancy chunks on heuristic boundaries.

Wired into ingestion (ingest_telegram.normalize / store path) so each chunk
becomes its OWN work_item. When a post looks like a digest but cannot be split
reliably, the caller SKIPS it (with a logged reason) rather than emitting one
contaminated item.

No I/O, no clock, no DB, no network.
"""

from __future__ import annotations

import re
from typing import List

# Explicit separator lines authors use between bundled vacancies.
# A line made (almost) entirely of these glyphs is treated as a boundary.
_SEP_LINE_RE = re.compile(
    r"^\s*(?:[-–—_=*•·▪️▫️◾◽⬛️★☆～~]{3,}|—{2,}|={3,})\s*$"
)

# A vacancy "header" line: a leading number/bullet/emoji followed by a role-ish
# title. Used both to count vacancies and as a split boundary when no explicit
# separator lines exist.
_HEADER_LINE_RE = re.compile(
    r"^\s*(?:"
    r"\d{1,2}[.)]\s+"                       # "1. ", "2) "
    r"|[🔹🔸▪️◾💼📌➡️👉🟢🟡🔴]\s*"            # bullet/emoji marker
    r")\s*\S"
)

# Markers that strongly indicate "a vacancy is described here". Counting how
# many appear is a cheap signal for multiplicity.
_VACANCY_MARKERS = [
    r"зарплат\w*", r"salary", r"оклад", r"вилка",
    r"\bremote\b", r"удал[её]нк\w*",
    r"откликнуться", r"подробнее", r"apply\b", r"\bваканси\w*", r"\bvacanc\w+",
]
_MARKER_RE = re.compile("|".join(_VACANCY_MARKERS), re.I)

# getmatch-style signal: the bundle often names its own digest nature.
_DIGEST_HINT_RE = re.compile(
    r"подборк\w*|дайджест|digest|подобрал\w*|вакансии недели|\bтоп[- ]?\d+\b|свежие вакансии",
    re.I,
)

_MIN_CHUNK_CHARS = 30  # a real vacancy chunk has at least this much text


def _vacancy_chunks(chunks: List[str]) -> List[str]:
    """Keep only chunks that INDEPENDENTLY look like a whole vacancy. PURE.

    The discriminator between a real multi-vacancy digest and a SINGLE vacancy
    that merely uses several bulleted SECTION headers (e.g. a job post with
    '🔹Что предстоит делать', '🔹Что важно', '🔹Что мы предлагаем') is whether
    EACH chunk carries its own vacancy signal. In a real getmatch-style digest,
    every numbered/separated item has its own salary / contact / apply marker;
    in a single vacancy the markers concentrate in ONE section (e.g. only the
    «Что мы предлагаем» block mentions удалёнка/оклад), while the other section
    headers carry none. So we count only chunks that are both long enough AND
    contain at least one vacancy marker.
    """
    return [
        c for c in chunks
        if len(c) >= _MIN_CHUNK_CHARS and _MARKER_RE.search(c)
    ]


def _split_on_separator_lines(text: str) -> List[str]:
    """Split where a whole line is a separator glyph run. PURE."""
    chunks: List[str] = []
    current: List[str] = []
    for line in text.splitlines():
        if _SEP_LINE_RE.match(line):
            if current:
                chunks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        chunks.append("\n".join(current).strip())
    return [c for c in chunks if c]


def _split_on_header_lines(text: str) -> List[str]:
    """Split before each numbered/bulleted vacancy header line. PURE."""
    lines = text.splitlines()
    # Indexes of header lines (skip a header on the very first line: that is the
    # digest's own title, not a vacancy boundary, unless others follow).
    header_idx = [i for i, ln in enumerate(lines) if _HEADER_LINE_RE.match(ln)]
    if len(header_idx) < 2:
        return [text.strip()] if text.strip() else []

    chunks: List[str] = []
    # Any preamble before the first header is the digest intro -> dropped.
    starts = header_idx
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        chunk = "\n".join(lines[start:end]).strip()
        if chunk:
            chunks.append(chunk)
    return chunks


def is_digest(text: str) -> bool:
    """Heuristic: True when ``text`` bundles several vacancies. PURE.

    Conservative: requires either explicit separator lines / multiple headers
    AND multiple vacancy markers, or an explicit digest hint plus multiplicity.
    """
    if not text or not text.strip():
        return False

    sep_chunks = _split_on_separator_lines(text)
    header_chunks = _split_on_header_lines(text)
    marker_count = len(_MARKER_RE.findall(text))

    # Multiplicity = how many chunks INDEPENDENTLY look like a vacancy (each
    # carries its own vacancy marker). This is what separates a true digest
    # (several self-contained vacancies) from one vacancy split across bulleted
    # SECTION headers (only one section carries markers) -- see _vacancy_chunks.
    multiplicity = max(
        len(_vacancy_chunks(sep_chunks)),
        len(_vacancy_chunks(header_chunks)),
    )

    if multiplicity >= 2 and marker_count >= 2:
        return True
    if _DIGEST_HINT_RE.search(text) and multiplicity >= 2:
        return True
    return False


def split_digest(text: str) -> List[str]:
    """Split a digest raw_text into per-vacancy chunks. PURE.

    Tries explicit separator lines first (most reliable), then numbered/bulleted
    headers. Returns the list of chunk texts. An EMPTY list means the digest
    could not be split reliably -> the caller should SKIP the post.
    """
    if not text or not text.strip():
        return []

    for splitter in (_split_on_separator_lines, _split_on_header_lines):
        chunks = [c for c in splitter(text) if len(c) >= _MIN_CHUNK_CHARS]
        # Only split when at least two chunks INDEPENDENTLY look like a vacancy
        # (each has its own marker). This prevents slicing a SINGLE vacancy that
        # happens to use several bulleted section headers into bogus pieces.
        if len(chunks) >= 2 and len(_vacancy_chunks(chunks)) >= 2:
            return chunks

    return []  # not reliably splittable
