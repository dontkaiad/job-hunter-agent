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

from . import llm, research_fetch
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
    """Gather company/role context for T10. Returns a research dict.

    Phase 1: BEFORE asking the LLM, attempt a direct-URL fetch of the vacancy
    permalink (and, best-effort, one company page) via ``research_fetch``. When
    usable page text is retrieved it is fed to the LLM as clearly-labeled real
    page text and ``research_source`` is "web"; otherwise we fall back to the
    desk-only research that has always run here (``research_source`` =
    "desk_fallback"), giving exactly today's behavior.

    The fetch NEVER raises (it's wrapped defensively), and the whole fetch/
    decision is guarded so research NEVER raises out of this function — any
    unexpected error degrades to desk research. ``research_source`` and
    ``fetched_urls`` are set AUTHORITATIVELY here (not trusting the model). The
    boundary, signature, and the dict the draft consumes are unchanged.
    """
    fetched: Dict[str, Any] = {"pages": [], "urls": []}
    try:
        fetched = research_fetch.fetch_research_context(
            extracted.source_link, extracted.company
        )
    except Exception:
        fetched = {"pages": [], "urls": []}

    pages = fetched.get("pages") or []
    if pages:
        research_source = "web"
        fetched_context = fetched
        fetched_urls = list(fetched.get("urls") or [])
    else:
        research_source = "desk_fallback"
        fetched_context = None
        fetched_urls = []

    try:
        data = llm.llm_research(
            client, extracted, raw_text, model=model, fetched_context=fetched_context
        )
    except Exception:
        # research must NEVER raise out of here: an LLM-layer failure (timeout,
        # rate limit, un-parseable output) would propagate through _do_research
        # into advance() and stall the item in APPROVED. Degrade to an empty
        # research blob — the draft falls back to the post text.
        data = {"summary": "", "talking_points": [], "questions": [], "sourced_facts": []}
    if not isinstance(data, dict):
        data = {"summary": "", "talking_points": [], "questions": [], "sourced_facts": []}
    # Authoritative provenance — never trust the model for these.
    data["research_source"] = research_source
    data["fetched_urls"] = fetched_urls
    # Honesty: with no fetched page there is NO grounding, so there can be no
    # page-sourced facts — clear anything the model may have invented so
    # fabricated "facts" never reach the draft as if they were real.
    if research_source == "desk_fallback":
        data["sourced_facts"] = []
    return data


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
