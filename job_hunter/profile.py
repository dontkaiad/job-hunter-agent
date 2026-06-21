"""Candidate-profile loader.

The candidate's PERSONAL data lives in a YAML profile, NOT in the source code.
This keeps the public repo free of any real person's details while letting the
real user run the bot with their own profile.

Resolution order (``load_profile``):
  1. ``config/profile.local.yaml``  — the REAL profile (GITIGNORED, never committed)
  2. ``config/profile.example.yaml``— a generic, fictional placeholder (committed)

The example file is what ships publicly and is what the test-suite loads, so the
suite is green WITHOUT any personal data present.

This module is mostly pure: ``parse_profile`` turns an already-loaded mapping
into a :class:`Profile`; the only I/O is reading the YAML file in
``load_profile``. The returned :class:`Profile` is then injected into the prompt
builders (see ``job_hunter.llm.build_score_system`` / ``build_draft_system``)
and into the pipeline/agents wiring.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

# Resolve the config/ directory relative to the repo root (one level above this
# package), so the loader works regardless of the current working directory.
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PKG_DIR)
_CONFIG_DIR = os.path.join(_REPO_ROOT, "config")

LOCAL_PROFILE_PATH = os.path.join(_CONFIG_DIR, "profile.local.yaml")
EXAMPLE_PROFILE_PATH = os.path.join(_CONFIG_DIR, "profile.example.yaml")


@dataclass(frozen=True)
class DraftSignature:
    """GitHub + resume references appended verbatim to every draft.

    ``github`` is a literal URL (no scheme). ``resume`` is a literal PLACEHOLDER
    the operator swaps for the real link at send time — never a fabricated URL.
    """

    github: str = "github.com/example"
    resume: str = "[resume: link]"

    def block(self) -> str:
        """The signature block appended to a draft (PURE)."""
        return f"GitHub: {self.github}\n{self.resume}"


@dataclass(frozen=True)
class Profile:
    """Loaded candidate profile. Pure data; injected into prompts + wiring."""

    role: str = ""
    target_grade: str = ""
    experience_note: str = ""
    hands_on: List[str] = field(default_factory=list)
    in_development: List[str] = field(default_factory=list)
    stack: List[str] = field(default_factory=list)
    languages: str = ""
    location_priority: List[str] = field(default_factory=list)
    gender: str = "unspecified"
    salary_floor_eur: float = 1000.0
    draft_signature: DraftSignature = field(default_factory=DraftSignature)
    # The path the profile was loaded from (None when built directly).
    source_path: Optional[str] = None


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    # A single scalar -> one-element list.
    s = str(value).strip()
    return [s] if s else []


def parse_profile(data: Mapping[str, Any], source_path: Optional[str] = None) -> Profile:
    """Turn an already-loaded mapping into a :class:`Profile`. PURE.

    Unknown keys are ignored; missing keys fall back to the dataclass defaults.
    """
    sig_raw = data.get("draft_signature") or {}
    if not isinstance(sig_raw, Mapping):
        sig_raw = {}
    sig = DraftSignature(
        github=_as_str(sig_raw.get("github")) or DraftSignature.github,
        resume=_as_str(sig_raw.get("resume")) or DraftSignature.resume,
    )

    floor_raw = data.get("salary_floor_eur")
    try:
        floor = float(floor_raw) if floor_raw is not None else Profile.salary_floor_eur
    except (TypeError, ValueError):
        floor = Profile.salary_floor_eur

    gender = _as_str(data.get("gender")).lower() or "unspecified"

    return Profile(
        role=_as_str(data.get("role")),
        target_grade=_as_str(data.get("target_grade")),
        experience_note=_as_str(data.get("experience_note")),
        hands_on=_as_list(data.get("hands_on")),
        in_development=_as_list(data.get("in_development")),
        stack=_as_list(data.get("stack")),
        languages=_as_str(data.get("languages")),
        location_priority=_as_list(data.get("location_priority")),
        gender=gender,
        salary_floor_eur=floor,
        draft_signature=sig,
        source_path=source_path,
    )


def load_profile_file(path: str) -> Profile:
    """Read and parse a single YAML profile file. I/O."""
    import yaml

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"profile file {path!r} must be a YAML mapping")
    return parse_profile(data, source_path=path)


def resolve_profile_path(
    local_path: str = LOCAL_PROFILE_PATH,
    example_path: str = EXAMPLE_PROFILE_PATH,
) -> str:
    """Return the local profile path if it exists, else the example path. PURE-ish.

    Only touches the filesystem to check existence.
    """
    if os.path.exists(local_path):
        return local_path
    return example_path


def load_profile(
    local_path: str = LOCAL_PROFILE_PATH,
    example_path: str = EXAMPLE_PROFILE_PATH,
) -> Profile:
    """Load the candidate profile: local (real) if present, else example.

    This is the single entry point the bot/pipeline call at startup.
    """
    return load_profile_file(resolve_profile_path(local_path, example_path))


# A lazily-cached example profile so the prompt module can render its module-level
# constants from the GENERIC profile without any I/O on every import in tests.
_EXAMPLE_CACHE: Dict[str, Profile] = {}


def example_profile() -> Profile:
    """The committed generic profile (used to render module-level prompt constants)."""
    cached = _EXAMPLE_CACHE.get("p")
    if cached is None:
        cached = load_profile_file(EXAMPLE_PROFILE_PATH)
        _EXAMPLE_CACHE["p"] = cached
    return cached
