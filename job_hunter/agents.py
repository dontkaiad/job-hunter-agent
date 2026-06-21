"""Agent(LLM) steps: research (T10) and draft (T11).

DESIGN.md §6 listed these as deferred pass-through stubs; in this build they are
REAL LLM-backed implementations. They take an injected LLMClient so the calling
pipeline stays testable with mocks.

Results are stored back into the item's extracted_json under reserved keys
('research', 'draft') so no schema/table change is needed.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from . import llm
from .llm import LLMClient
from .profile import Profile, example_profile
from .schema_extract import ExtractResult

# Deterministic links appended to EVERY draft. The отклик is sent MANUALLY by
# the operator (no auto-send), and the candidate's proof of skill is her GitHub
# + резюме. We do not trust the LLM to emit these reliably (it tends to drop or
# "slop" them away, or fabricate a fake URL), so they are appended verbatim
# AFTER generation. The actual link + placeholder come from the loaded PROFILE
# (config/profile.*.yaml), NOT hardcoded here. The module-level constants below
# are rendered from the GENERIC example profile so this module carries NO
# personal data; live runs pass the loaded local profile via ``draft(...,
# profile=...)``. GITHUB is a literal URL; RESUME is a literal PLACEHOLDER the
# operator swaps for the real link at send time (we never invent a resume URL).
_EXAMPLE_SIG = example_profile().draft_signature
GITHUB_LINK = _EXAMPLE_SIG.github
RESUME_PLACEHOLDER = _EXAMPLE_SIG.resume
_SIGNATURE_BLOCK = _EXAMPLE_SIG.block()


def signature_block(profile: Optional[Profile] = None) -> str:
    """The GitHub + резюме signature block for ``profile``. PURE.

    Defaults to the GENERIC example signature when no profile is given.
    """
    sig = (profile or example_profile()).draft_signature
    return sig.block()


def append_draft_signature(draft_text: str, profile: Optional[Profile] = None) -> str:
    """Append the deterministic GitHub + резюме signature to a draft. PURE.

    Guarantees both literal references are present even if the LLM omitted or
    mangled them. Idempotent: if the exact signature block is already at the end
    it is not duplicated. The GitHub link and резюме placeholder are taken from
    the loaded profile (generic example when None) — no real resume URL is ever
    fabricated here.
    """
    block = signature_block(profile)
    body = (draft_text or "").rstrip()
    if body.endswith(block):
        return body
    if not body:
        return block
    return f"{body}\n\n{block}"


def research(
    client: LLMClient,
    extracted: ExtractResult,
    raw_text: str,
    model: str = llm.CHEAP_MODEL,
) -> Dict[str, Any]:
    """Gather company/role context via the LLM (T10). Returns a research dict."""
    return llm.llm_research(client, extracted, raw_text, model=model)


def draft(
    client: LLMClient,
    extracted: ExtractResult,
    raw_text: str,
    research_data: Optional[Dict[str, Any]] = None,
    model: str = llm.CHEAP_MODEL,
    profile: Optional[Profile] = None,
) -> str:
    """Generate an application draft via the LLM (T11). Returns message text.

    The deterministic GitHub + резюме signature is appended after generation so
    the literal links are ALWAYS present regardless of what the model emits. The
    ``profile`` (when given) drives both the prompt and the signature; when None
    the GENERIC example profile is used.
    """
    body = llm.llm_draft(
        client, extracted, raw_text, research_data, model=model, profile=profile
    )
    return append_draft_signature(body, profile)


def merge_aux(extracted_json: Optional[str], key: str, value: Any) -> str:
    """Merge an auxiliary key (research/draft) into an extracted_json blob.

    PURE. Keeps the §4 schema fields intact and stores agent output alongside.
    """
    try:
        data = json.loads(extracted_json) if extracted_json else {}
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[key] = value
    return json.dumps(data, ensure_ascii=False, sort_keys=True)
