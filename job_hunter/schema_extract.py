"""PURE: the Extract output contract (DESIGN.md §4).

ExtractResult dataclass + validate / parse / serialize. No I/O.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# contact_type enum (SCORING.md): dm | form | link, unknown -> None.
CONTACT_TYPES = frozenset({"dm", "form", "link"})


@dataclass
class ExtractResult:
    """Structured fields parsed from a raw job post.

    Stored as JSON in ``work_items.extracted_json``. Unknown values are None.
    ``stack``, ``reasons`` and ``benefits`` are always present (possibly empty
    lists).

    ``benefits`` is a DISPLAY-ONLY list of canonical perks/conditions parsed
    from the post (the «Что мы предлагаем» block / hashtags / body). It lives
    in the extracted_json blob (NOT a DB column) and has ZERO scoring impact:
    it is never part of the score payload (see llm.build_score_prompt) and is
    not referenced by scoring.py.
    """

    title: str
    source_channel: str
    company: Optional[str] = None
    stack: List[str] = field(default_factory=list)
    seniority: Optional[str] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    currency: Optional[str] = None
    remote: Optional[bool] = None
    relocation: Optional[bool] = None
    location: Optional[str] = None
    contact_type: Optional[str] = None
    contact: Optional[str] = None
    source_link: Optional[str] = None
    relevance_score: Optional[float] = None
    reasons: List[str] = field(default_factory=list)
    # Display-only perks/conditions (canonical labels in the post's language).
    # Default [] so old extracted_json rows without this key still load.
    benefits: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


# Field name -> (python type(s), allow_none)
_NUMERIC = (int, float)
_FIELD_SPEC = {
    "title": (str, False),
    "source_channel": (str, False),
    "company": (str, True),
    "seniority": (str, True),
    "salary_min": (_NUMERIC, True),
    "salary_max": (_NUMERIC, True),
    "currency": (str, True),
    "remote": (bool, True),
    "relocation": (bool, True),
    "location": (str, True),
    "contact_type": (str, True),
    "contact": (str, True),
    "source_link": (str, True),
    "relevance_score": (_NUMERIC, True),
}


def validate(result: ExtractResult) -> List[str]:
    """Return a list of validation warnings. Empty list = valid.

    Validation never raises for soft problems; it reports them. A few shape
    problems (wrong python types) ARE surfaced here as warnings so callers can
    decide. salary_min <= salary_max is a warning, not a hard reject (§4).
    """
    warnings: List[str] = []
    d = result.to_dict()

    for name, (types, allow_none) in _FIELD_SPEC.items():
        val = d.get(name)
        if val is None:
            if not allow_none:
                warnings.append(f"{name} must not be null")
            continue
        # bool is a subclass of int; guard numeric fields against bool.
        if types is _NUMERIC and isinstance(val, bool):
            warnings.append(f"{name} must be a number, got bool")
            continue
        if not isinstance(val, types):
            warnings.append(f"{name} has wrong type: {type(val).__name__}")

    for list_name in ("stack", "reasons", "benefits"):
        val = getattr(result, list_name)
        if not isinstance(val, list):
            warnings.append(f"{list_name} must be a list")
        elif not all(isinstance(x, str) for x in val):
            warnings.append(f"{list_name} must contain only strings")

    smin, smax = result.salary_min, result.salary_max
    if smin is not None and smax is not None and smin > smax:
        warnings.append("salary_min > salary_max")

    if result.contact_type is not None and result.contact_type not in CONTACT_TYPES:
        warnings.append(
            f"contact_type '{result.contact_type}' not in {sorted(CONTACT_TYPES)}"
        )

    return warnings


def is_valid(result: ExtractResult) -> bool:
    return not validate(result)


def from_dict(data: Dict[str, Any]) -> ExtractResult:
    """Build an ExtractResult from a plain dict, tolerating missing keys.

    Unknown keys are ignored. Missing keys fall back to dataclass defaults.
    ``stack``/``reasons``/``benefits`` are coerced to lists (``benefits``
    defaults to [] for back-compat with old rows lacking the key).
    """
    known = {f for f in ExtractResult.__dataclass_fields__}  # type: ignore[attr-defined]
    kwargs: Dict[str, Any] = {k: v for k, v in data.items() if k in known}

    # Required fields with sane fallbacks so parse never explodes on noise.
    kwargs.setdefault("title", "")
    kwargs.setdefault("source_channel", "")

    for list_name in ("stack", "reasons", "benefits"):
        v = kwargs.get(list_name)
        if v is None:
            kwargs[list_name] = []
        elif isinstance(v, str):
            kwargs[list_name] = [v]
        elif not isinstance(v, list):
            kwargs[list_name] = list(v)

    return ExtractResult(**kwargs)


def parse(text: str) -> ExtractResult:
    """Parse a JSON string into an ExtractResult."""
    return from_dict(json.loads(text))


def serialize(result: ExtractResult) -> str:
    return result.to_json()
