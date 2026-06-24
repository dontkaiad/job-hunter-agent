"""Loader for the de-identification denylist used by the profile guard tests.

The actual personal needles live in tests/_personal_needles.local.py, which is
GITIGNORED -- so no personal string ever enters the tracked repo. On the
maintainer's machine that file is present and the guard tests assert the
rendered profile / SCORE_SYSTEM / DRAFT_SYSTEM contain none of those strings. On
a fresh clone or CI the file is absent, so personal_needles() returns [] and
personal_needle() returns a sentinel that matches nothing: the guards then pass
vacuously, which is correct -- a clean checkout has no personal data to leak.
"""
from __future__ import annotations

import importlib.util
import os
from typing import List

_NEVER_MATCH = "__ABSENT_PERSONAL_NEEDLE__"


def _load():
    path = os.path.join(os.path.dirname(__file__), "_personal_needles.local.py")
    if not os.path.exists(path):
        return [], {}
    spec = importlib.util.spec_from_file_location("_personal_needles_local", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, "FRAGMENTS", []), getattr(mod, "NEEDLES", {})


_FRAGMENTS, _NEEDLES = _load()


def personal_needles() -> List[str]:
    """Full personal denylist (joined). Empty on a fresh clone."""
    return ["".join(parts) for parts in _FRAGMENTS]


def personal_needle(key: str) -> str:
    """One personal needle by key, or a never-matching sentinel if the local
    fixture is absent (fresh clone)."""
    parts = _NEEDLES.get(key)
    return "".join(parts) if parts else _NEVER_MATCH
