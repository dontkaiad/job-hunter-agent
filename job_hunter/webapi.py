"""READ-ONLY FastAPI dashboard for the job-hunter pipeline (issue #3).

This module exposes the pipeline state over HTTP for a dashboard. It is
STRICTLY READ-ONLY: it never advances state, never writes, never calls the LLM
or mutates FX. It reads from the SAME PostgreSQL the bot uses (config
``cfg.database_url``) via the SAME synchronous psycopg v3 driver — no async DB.

Structure (so issue #5 auth can wrap this WITHOUT a refactor):
  - ``create_app()`` — app factory returning the FastAPI app.
  - ``router`` — an ``APIRouter`` holding the endpoints.
  - ``get_conn`` — a FastAPI dependency yielding a psycopg connection. Tests
    override it via ``app.dependency_overrides``; auth (#5) composes with it.
  - ``get_fx`` — a FastAPI dependency yielding the cached FX rates object.

Field-mapping notes:
  - ``score`` is the authoritative ``work_items.relevance_score`` column (NOT
    the score embedded in extracted_json).
  - ``role`` maps to ``ExtractResult.title``.
  - ``salary.display`` is the bot-card ₽ string. We reuse the bot's salary
    helpers (``bot._salary_rub``-equivalent logic + ``fx.format_rub``) so the
    dashboard matches the Telegram card exactly. Currency logic is NOT
    reimplemented here.
  - ``published_at`` == ``work_items.created_at``. NOTE: there is NO per-item
    publication-date column. ``created_at`` is INGESTION time, not the true
    post date. Surfacing the true post date would need a schema change and is
    OUT OF SCOPE for this read-only issue.

processed (разобрано / неразобрано) — JUDGMENT CALL (tweakable):
  processed=false (неразобрано) => state IN {discovered, extracted, scored,
    surfaced}: still in the review inbox / automated pipeline.
  processed=true  (разобрано)   => everything else {skipped, backlog, approved,
    researched, drafted, sent, closed, rejected}: a human acted or it is
    resolved. Implemented as a SQL ``state IN (...)`` partition in
    ``store.list_pipeline``.
"""

from __future__ import annotations

import json
from typing import Iterator, List, Optional

import psycopg
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel

from . import fx as fx_mod
from . import store
from .config import Config, load_config
from .pipeline import REASONING_KEY
from .schema_extract import ExtractResult, from_dict

# Generous cap on the flat pipeline list (the dashboard is not paginated yet).
_PIPELINE_LIMIT = 1000


# --- Pydantic v2 response models --------------------------------------------


class Salary(BaseModel):
    min: Optional[float] = None
    max: Optional[float] = None
    currency: Optional[str] = None
    # The bot-card ₽-equivalent string (e.g. "~290k ₽"); null when FX is
    # unavailable or the amount/currency cannot be converted.
    display: Optional[str] = None


class Source(BaseModel):
    channel: Optional[str] = None
    link: Optional[str] = None


class PipelineItem(BaseModel):
    id: int
    score: Optional[float] = None
    company: Optional[str] = None
    role: Optional[str] = None  # = ExtractResult.title
    stack: List[str] = []
    remote: Optional[bool] = None
    salary: Salary
    status: str  # = work_items.state
    published_at: Optional[str] = None  # = created_at (INGESTION time)
    source: Source


class Transition(BaseModel):
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    kind: Optional[str] = None
    actor: Optional[str] = None
    reason: Optional[str] = None
    created_at: Optional[str] = None


class ItemDetail(BaseModel):
    id: int
    status: str  # = state
    score: Optional[float] = None
    # Full ExtractResult fields.
    title: Optional[str] = None
    company: Optional[str] = None
    stack: List[str] = []
    seniority: Optional[str] = None
    salary: Salary
    remote: Optional[bool] = None
    relocation: Optional[bool] = None
    location: Optional[str] = None
    contact_type: Optional[str] = None
    contact: Optional[str] = None
    reasons: List[str] = []
    benefits: List[str] = []
    # Reserved extracted_json keys (tolerated missing on old rows).
    reasoning: Optional[str] = None  # «Обоснование»
    draft: Optional[str] = None
    research: Optional[object] = None  # dict | str | None
    raw_text: Optional[str] = None
    source: Source
    published_at: Optional[str] = None  # = created_at (INGESTION time)
    updated_at: Optional[str] = None
    history: List[Transition] = []


# --- Salary display (reuse the bot's FX + format helper) --------------------


def _salary_display(ex: ExtractResult, fx: Optional[fx_mod.FxRates]) -> Optional[str]:
    """Return the bot-card ₽ string for ``ex``, or None.

    Mirrors ``bot.JobBot._salary_rub`` (convert the top = max-or-min amount) and
    renders it with ``fx.format_rub`` — the SAME helper the surfaced card uses,
    so the dashboard ₽ matches the bot. Returns None when FX is unavailable or
    the amount/currency is missing/unconvertible.
    """
    if fx is None:
        return None
    top = ex.salary_max if ex.salary_max is not None else ex.salary_min
    if top is None or ex.currency is None:
        return None
    try:
        rub = fx.convert(top, ex.currency)
    except Exception:
        return None
    if rub is None:
        return None
    return fx_mod.format_rub(rub)


def _parse_extracted(raw_json: Optional[str]) -> tuple:
    """Parse extracted_json into (ExtractResult, raw_dict). Tolerant of noise.

    Returns ``(ExtractResult, {})`` on missing/garbage JSON so old or empty rows
    never break the endpoint. The raw dict is used to read the reserved keys
    («Обоснование» / "draft" / "research") that are merged alongside the schema.
    """
    if not raw_json:
        return from_dict({}), {}
    try:
        data = json.loads(raw_json)
    except (ValueError, TypeError):
        return from_dict({}), {}
    if not isinstance(data, dict):
        return from_dict({}), {}
    return from_dict(data), data


# --- Dependencies (overridable; composable with auth in #5) -----------------


def get_config() -> Config:
    """Resolve the runtime Config (DATABASE_URL etc.). Overridable in tests."""
    return load_config()


def get_conn(config: Config = Depends(get_config)) -> Iterator[psycopg.Connection]:
    """Yield a READ-ONLY psycopg connection from ``cfg.database_url``.

    A new connection per request, closed after. The endpoints never commit; we
    rollback on teardown to drop any implicit read transaction. Tests override
    this dependency to inject the ephemeral test connection.
    """
    conn = store.connect(config.database_url)
    try:
        yield conn
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()


def get_fx(config: Config = Depends(get_config)) -> fx_mod.FxRates:
    """Yield the cached FX rates object. Overridden with a fake (no network) in
    tests. Reused — never mutated — by the endpoints."""
    return fx_mod.FxRates(provider=config.fx_provider, cache_ttl=config.fx_cache_ttl)


# --- Router / endpoints -----------------------------------------------------

router = APIRouter(prefix="/api", tags=["pipeline"])


@router.get("/pipeline", response_model=List[PipelineItem])
def get_pipeline(
    status: Optional[str] = Query(default=None, description="Exact state match"),
    min_score: Optional[float] = Query(
        default=None, description="relevance_score >= min_score (NULL scores excluded)"
    ),
    remote: Optional[bool] = Query(default=None, description="Filter on extracted remote flag"),
    processed: Optional[bool] = Query(
        default=None,
        description="true=разобрано (human acted/resolved); false=неразобрано (inbox/pipeline)",
    ),
    q: Optional[str] = Query(
        default=None, description="Case-insensitive substring over company + role + stack"
    ),
    conn: psycopg.Connection = Depends(get_conn),
    fx: fx_mod.FxRates = Depends(get_fx),
) -> List[PipelineItem]:
    """Flat pipeline list for the dashboard table/lanes. READ-ONLY.

    Column-backed filters (status / min_score / processed) are applied in SQL by
    ``store.list_pipeline``. blob-backed filters (remote / q) are applied here in
    Python after parsing extracted_json (it is TEXT, not jsonb).
    """
    items = store.list_pipeline(
        conn,
        status=status,
        min_score=min_score,
        processed=processed,
        limit=_PIPELINE_LIMIT,
    )

    # Strip first: a whitespace-only q (e.g. "%20") is treated as no filter,
    # not as a literal-space substring that would match every multi-word field.
    q_lower = q.strip().lower() if q and q.strip() else None
    out: List[PipelineItem] = []
    for item in items:
        ex, _ = _parse_extracted(item.extracted_json)

        # remote filter (parsed from the blob; tri-state None passes through).
        if remote is not None and ex.remote is not remote:
            continue

        # q substring over company + role(title) + stack entries (case-insensitive).
        if q_lower is not None:
            haystack = " ".join(
                [ex.company or "", ex.title or ""] + [s for s in ex.stack if s]
            ).lower()
            if q_lower not in haystack:
                continue

        out.append(
            PipelineItem(
                id=item.id,
                score=item.relevance_score,  # authoritative column
                company=ex.company,
                role=ex.title or None,
                stack=ex.stack,
                remote=ex.remote,
                salary=Salary(
                    min=ex.salary_min,
                    max=ex.salary_max,
                    currency=ex.currency,
                    display=_salary_display(ex, fx),
                ),
                status=item.state,
                published_at=item.created_at,  # INGESTION time (see module docstring)
                source=Source(channel=item.source_channel, link=item.source_link),
            )
        )
    return out


@router.get("/items/{item_id}", response_model=ItemDetail)
def get_item_detail(
    item_id: int,
    conn: psycopg.Connection = Depends(get_conn),
    fx: fx_mod.FxRates = Depends(get_fx),
) -> ItemDetail:
    """Full detail for one item incl. reasoning, draft, research and the state
    transition history. READ-ONLY. 404 when the id does not exist."""
    item = store.get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"work item {item_id} not found")

    ex, data = _parse_extracted(item.extracted_json)

    reasoning = data.get(REASONING_KEY)
    draft = data.get("draft")
    research = data.get("research")

    history = [
        Transition(
            from_state=t.get("from_state"),
            to_state=t.get("to_state"),
            kind=t.get("kind"),
            actor=t.get("actor"),
            reason=t.get("reason"),
            created_at=t.get("created_at"),
        )
        for t in store.list_transitions(conn, item_id)
    ]

    return ItemDetail(
        id=item.id,
        status=item.state,
        score=item.relevance_score,
        title=ex.title or None,
        company=ex.company,
        stack=ex.stack,
        seniority=ex.seniority,
        salary=Salary(
            min=ex.salary_min,
            max=ex.salary_max,
            currency=ex.currency,
            display=_salary_display(ex, fx),
        ),
        remote=ex.remote,
        relocation=ex.relocation,
        location=ex.location,
        contact_type=ex.contact_type,
        contact=ex.contact,
        reasons=ex.reasons,
        benefits=ex.benefits,
        reasoning=reasoning if isinstance(reasoning, str) else None,
        draft=draft if isinstance(draft, str) else None,
        research=research,
        raw_text=item.raw_text,
        source=Source(channel=item.source_channel, link=item.source_link),
        published_at=item.created_at,  # INGESTION time (see module docstring)
        updated_at=item.updated_at,
        history=history,
    )


# --- App factory ------------------------------------------------------------


def create_app() -> FastAPI:
    """Build the READ-ONLY dashboard FastAPI app.

    Issue #5 (auth) can later add an auth dependency to ``router`` (via
    ``dependencies=[Depends(require_auth)]``) or wrap ``get_conn`` WITHOUT
    changing endpoint code — that is why everything goes through the factory,
    the router, and overridable dependencies.
    """
    app = FastAPI(title="Job Hunter Dashboard API", version="1.0.0")
    app.include_router(router)
    return app


app = create_app()
