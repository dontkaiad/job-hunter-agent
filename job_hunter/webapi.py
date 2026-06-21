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
import os
from dataclasses import dataclass
from typing import Iterator, List, Optional, Set

import psycopg
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import fx as fx_mod
from . import pipeline as pipeline_mod
from . import store
from . import tg_auth
from .config import Config, load_config
from .pipeline import REASONING_KEY, Deps
from .schema_extract import ExtractResult, from_dict
from .states import (
    APPROVED,
    DECISION_APPROVE,
    DECISION_BACKLOG,
    DECISION_SEND,
    DECISION_SKIP,
    DRAFTED,
    RESEARCHED,
    transition_for_decision,
)

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
    """Yield a psycopg connection from ``cfg.database_url`` (one per request).

    Used by BOTH the read routes (#3) and the action/write routes (#4). The
    GET endpoints never write; the action endpoints mutate state ONLY via
    ``pipeline.advance_by_id`` / ``pipeline.run_to_gate``, which commit
    internally (the store update runs with commit enabled). The teardown
    rollback is therefore a no-op after a committed write and simply drops any
    uncommitted read transaction otherwise. A new connection per request keeps
    the API concurrency-safe with the bot at the PG level. Tests override this
    dependency to inject the ephemeral test connection.
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


# --- Auth (issue #5): Telegram-Login-Widget + SSO cookie + grants ------------
#
# The reusable PRIMITIVES live in job_hunter.tg_auth (framework-agnostic). This
# section is the jobhunter-specific FastAPI GLUE: it threads the config secrets
# into those primitives via overridable dependencies and gates every /api route.

# The app key this dashboard authorizes against (grants.app). Per-app opt-in:
# only routers that Depends(require_auth(app=...)) are gated; another app reusing
# tg_auth would pass its own app name and stays independent.
APP_NAME = "jobhunter"

# Session cookie name. Shared across heylark apps (same Domain) so it is a
# single-sign-on token.
SESSION_COOKIE = "hl_session"

# Templates for the public /login page (HTML kept OUT of this module).
_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)


@dataclass
class AuthSettings:
    """Resolved auth knobs, injected as ONE dependency so tests can override the
    secret / superuser set / login-bot token together. NEVER hardcoded — all
    values come from Config (env)."""

    session_secret: str
    superuser_ids: Set[int]
    login_bot_token: str
    login_bot_username: str
    cookie_domain: str
    auth_database_url: str


def get_auth_settings(config: Config = Depends(get_config)) -> AuthSettings:
    """Build AuthSettings from Config. Overridden in tests to inject a test
    secret, a test superuser set and a fake login-bot token."""
    return AuthSettings(
        session_secret=config.session_secret or "",
        superuser_ids=set(config.superuser_tg_ids),
        login_bot_token=config.tg_login_bot_token or "",
        login_bot_username=config.tg_login_bot_username or "",
        cookie_domain=config.cookie_domain,
        auth_database_url=config.auth_database_url,
    )


def get_auth_conn(
    settings: AuthSettings = Depends(get_auth_settings),
) -> Iterator[psycopg.Connection]:
    """Yield a connection to the SEPARATE auth Postgres (AUTH_DATABASE_URL),
    closed after the request. Overridden in tests to inject the ephemeral
    auth-DB connection with seeded grants. This is a DIFFERENT database from the
    pipeline DB (get_conn) — it never touches pipeline state."""
    conn = tg_auth.connect_auth(settings.auth_database_url)
    try:
        yield conn
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()


def require_auth(app: str):
    """Dependency FACTORY: returns a FastAPI dependency that authenticates the
    session cookie and authorizes the tg_id against ``app``'s grants.

    Behaviour:
      1. No/invalid session cookie -> 401 for /api/* (JSON). For a non-/api HTML
         route it 307-redirects to /login instead (so an HTML page could reuse
         this same gate). All currently-gated routes are /api, so they 401.
      2. Valid tg_id but not authorized (no approved grant, or pending/denied)
         -> 403.
      3. Authorized -> returns the tg_id (endpoints may consume it).
    """

    def dependency(
        request: Request,
        settings: AuthSettings = Depends(get_auth_settings),
        auth_conn: psycopg.Connection = Depends(get_auth_conn),
    ) -> int:
        token = request.cookies.get(SESSION_COOKIE)
        tg_id = (
            tg_auth.read_session(token, secret=settings.session_secret)
            if token
            else None
        )
        if tg_id is None:
            # API paths return JSON 401; HTML pages would redirect to /login.
            if request.url.path.startswith("/api"):
                raise HTTPException(status_code=401, detail="authentication required")
            raise HTTPException(
                status_code=307,
                detail="redirect to login",
                headers={"Location": "/login"},
            )
        if not tg_auth.authorize(
            auth_conn, tg_id, app, superuser_ids=settings.superuser_ids
        ):
            raise HTTPException(status_code=403, detail="not authorized for this app")
        return tg_id

    return dependency


# --- Router / endpoints -----------------------------------------------------

router = APIRouter(
    prefix="/api",
    tags=["pipeline"],
    dependencies=[Depends(require_auth(app=APP_NAME))],
)


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


def _build_item_detail(
    conn: psycopg.Connection,
    item: store.WorkItem,
    fx: Optional[fx_mod.FxRates],
) -> ItemDetail:
    """Serialize a WorkItem into the full ItemDetail (the #3 detail shape).

    Shared by the READ-ONLY ``GET /items/{id}`` and the WRITE/action endpoints so
    every "return the updated item" response uses the IDENTICAL serializer (with
    the new status + draft text + «Обоснование» + history). Callers that mutate
    state MUST re-read the item (``store.get_item``) AFTER the transition and pass
    the fresh row here.
    """
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
        for t in store.list_transitions(conn, item.id)
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
    return _build_item_detail(conn, item, fx)


# --- WRITE / action endpoints (issue #4) ------------------------------------
#
# SOLE-WRITER DISCIPLINE: these endpoints NEVER write state directly. Every
# mutation goes through the EXACT pipeline functions the Telegram bot uses —
# ``pipeline.advance_by_id`` (HITL decisions) and ``pipeline.run_to_gate`` (the
# agent T10/T11 chain) — so ``advance()`` remains the SOLE writer of
# work_items.state. No ``store.update_state`` / no direct state SQL lives here.
#
# Action -> bot button -> decision -> transition map (mirrors bot.ACTION_TO_
# DECISION + states.TRANSITIONS):
#   approve  -> «✅» -> DECISION_APPROVE -> T7 surfaced->approved / T8 backlog->approved
#   skip     -> «⏭️» -> DECISION_SKIP    -> T5 surfaced->skipped  / T9 backlog->skipped
#   backlog  -> «📥» -> DECISION_BACKLOG -> T6 surfaced->backlog
#   sent     -> «✅ Отправила» -> DECISION_SEND -> T12 drafted->sent
#   draft    -> (no single button) run_to_gate -> T10 approved->researched,
#               T11 researched->drafted (the SAME chain the bot fires after a
#               successful approve).
#
# APPROVE / DRAFT SPLIT: the bot FUSES approve + auto-draft in handle_callback
# (it runs run_to_gate right after a successful approve and then notifies the
# draft). For the dashboard we SPLIT these into two routes that call the
# IDENTICAL underlying functions: POST /approve performs ONLY the approve
# transition (item ends in 'approved'); POST /draft is the separate step that
# runs the SAME run_to_gate to drive approved->researched->drafted. This suits a
# dashboard where the operator approves first, then explicitly generates the
# отклик (and avoids an implicit LLM call on every approve).


def get_deps(config: Config = Depends(get_config)) -> Deps:
    """Build the SAME Deps bundle the bot uses (LLM client + FX + profile).

    Reuses ``bot.build_deps(cfg)`` so the API's research/draft steps run through
    the identical collaborators as the Telegram path. Overridden in tests to
    inject a FAKE llm_client/fx so NO network/auth call happens.
    """
    from .bot import build_deps

    return build_deps(config)


# Writer router: gated by the SAME require_auth as the read router (issue #5
# folds the #4 no-op require_writer into require_auth). Every /api route — the
# GETs and all 5 POST actions — is now behind auth+authz.
writer_router = APIRouter(
    prefix="/api",
    tags=["actions"],
    dependencies=[Depends(require_auth(app=APP_NAME))],
)


# Maps the action route -> the HITL decision the bot's button sends.
_DECISION_LABELS = {
    DECISION_APPROVE: "approve",
    DECISION_SKIP: "skip",
    DECISION_BACKLOG: "backlog",
    DECISION_SEND: "sent",
}


def _apply_decision(
    conn: psycopg.Connection,
    item_id: int,
    decision: str,
    fx: Optional[fx_mod.FxRates],
    deps: Deps,
) -> ItemDetail:
    """Run a single HITL decision through advance_by_id and serialize the result.

    advance() is the SOLE transition guard: we DO NOT pre-check legality, we
    inspect the AdvanceResult. ``status == 'moved'`` -> 200 with the re-read item.
    Any non-moved status ('needs_human' / 'terminal' / 'noop') is an invalid
    transition for the current state -> 409 Conflict naming the state + action.
    404 when the id is unknown.
    """
    item = store.get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"work item {item_id} not found")

    current_state = item.state
    result = pipeline_mod.advance_by_id(conn, item_id, decision=decision, deps=deps)

    if result.status != "moved":
        label = _DECISION_LABELS.get(decision, decision)
        # transition_for_decision gives a nicer message, but advance() is the gate.
        legal = transition_for_decision(current_state, decision)
        hint = "" if legal else f" (no '{label}' transition from '{current_state}')"
        raise HTTPException(
            status_code=409,
            detail=(
                f"cannot '{label}' item {item_id} in state '{current_state}'"
                f"{hint}: advance returned '{result.status}'"
            ),
        )

    # Re-read AFTER the transition (advance committed internally) for the response.
    updated = store.get_item(conn, item_id)
    if updated is None:  # pragma: no cover - just-moved item cannot vanish
        raise HTTPException(status_code=404, detail=f"work item {item_id} not found")
    return _build_item_detail(conn, updated, fx)


@writer_router.post("/items/{item_id}/approve", response_model=ItemDetail)
def approve_item(
    item_id: int,
    conn: psycopg.Connection = Depends(get_conn),
    fx: fx_mod.FxRates = Depends(get_fx),
    deps: Deps = Depends(get_deps),
) -> ItemDetail:
    """DECISION_APPROVE: T7 surfaced->approved or T8 backlog->approved.

    ONLY the approve transition (split from auto-draft; see module note). The
    item ends in 'approved'; call POST /draft next to generate the отклик."""
    return _apply_decision(conn, item_id, DECISION_APPROVE, fx, deps)


@writer_router.post("/items/{item_id}/skip", response_model=ItemDetail)
def skip_item(
    item_id: int,
    conn: psycopg.Connection = Depends(get_conn),
    fx: fx_mod.FxRates = Depends(get_fx),
    deps: Deps = Depends(get_deps),
) -> ItemDetail:
    """DECISION_SKIP: T5 surfaced->skipped or T9 backlog->skipped."""
    return _apply_decision(conn, item_id, DECISION_SKIP, fx, deps)


@writer_router.post("/items/{item_id}/backlog", response_model=ItemDetail)
def backlog_item(
    item_id: int,
    conn: psycopg.Connection = Depends(get_conn),
    fx: fx_mod.FxRates = Depends(get_fx),
    deps: Deps = Depends(get_deps),
) -> ItemDetail:
    """DECISION_BACKLOG: T6 surfaced->backlog."""
    return _apply_decision(conn, item_id, DECISION_BACKLOG, fx, deps)


@writer_router.post("/items/{item_id}/sent", response_model=ItemDetail)
def sent_item(
    item_id: int,
    conn: psycopg.Connection = Depends(get_conn),
    fx: fx_mod.FxRates = Depends(get_fx),
    deps: Deps = Depends(get_deps),
) -> ItemDetail:
    """DECISION_SEND: T12 drafted->sent (the bot's «✅ Отправила» manual-confirm)."""
    return _apply_decision(conn, item_id, DECISION_SEND, fx, deps)


@writer_router.post("/items/{item_id}/draft", response_model=ItemDetail)
def draft_item(
    item_id: int,
    conn: psycopg.Connection = Depends(get_conn),
    fx: fx_mod.FxRates = Depends(get_fx),
    deps: Deps = Depends(get_deps),
) -> ItemDetail:
    """Drive an approved/researched item to DRAFTED via the SAME run_to_gate the
    bot uses (T10 research + T11 draft, both through advance()). Returns the
    updated item incl. the отклик (draft text from extracted_json "draft").

    - approved / researched -> run_to_gate -> expect DRAFTED -> 200.
    - already 'drafted'      -> idempotent: return the existing draft, NO LLM rerun.
    - any other state        -> 409 (cannot draft from there).
    """
    item = store.get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"work item {item_id} not found")

    if item.state == DRAFTED:
        # Idempotent: do NOT re-run the LLM; return the already-generated draft.
        return _build_item_detail(conn, item, fx)

    if item.state not in (APPROVED, RESEARCHED):
        raise HTTPException(
            status_code=409,
            detail=(
                f"cannot draft item {item_id} in state '{item.state}': "
                f"draft requires 'approved' or 'researched'"
            ),
        )

    # SAME agent chain the bot runs after an approve. advance() stays sole writer.
    pipeline_mod.run_to_gate(conn, item_id, deps=deps)

    updated = store.get_item(conn, item_id)
    if updated is None:  # pragma: no cover
        raise HTTPException(status_code=404, detail=f"work item {item_id} not found")
    if updated.state != DRAFTED:
        # run_to_gate could not reach DRAFTED (e.g. no LLM client wired). Surface
        # a clear conflict rather than a misleading 200 with no draft.
        raise HTTPException(
            status_code=409,
            detail=(
                f"draft for item {item_id} did not reach 'drafted' "
                f"(stopped at '{updated.state}')"
            ),
        )
    return _build_item_detail(conn, updated, fx)


# --- Public auth routes (issue #5): NOT gated -------------------------------
#
# These three routes are the login flow and MUST be reachable without a session.
# They live on a separate router with NO require_auth dependency.

auth_router = APIRouter(tags=["auth"])

# Where to send the user after a successful login. Configurable later; "/" today.
POST_LOGIN_PATH = "/"


def _set_session_cookie(
    response, token: str, max_age: Optional[int], settings: AuthSettings
) -> None:
    """Set the SSO session cookie with the cross-subdomain attributes.

    Domain=COOKIE_DOMAIN (.heylark.dev), HttpOnly, Secure, SameSite=Lax. max_age
    is ~30d for "remember me" or None for a session cookie (browser-lifetime).
    """
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=max_age,
        domain=settings.cookie_domain,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


@auth_router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    settings: AuthSettings = Depends(get_auth_settings),
) -> HTMLResponse:
    """Public login page rendering the Telegram Login Widget (uses the login
    bot's USERNAME) + a remember-me checkbox. Template lives in
    job_hunter/templates/login.html."""
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "bot_username": settings.login_bot_username,
            "callback_url": "/auth/callback",
        },
    )


@auth_router.get("/auth/callback")
def auth_callback(
    request: Request,
    settings: AuthSettings = Depends(get_auth_settings),
    auth_conn: psycopg.Connection = Depends(get_auth_conn),
) -> RedirectResponse:
    """Telegram Login Widget redirect target. Verifies the signed payload,
    authorizes the grant, then mints + sets the SSO cookie and redirects.

    Invalid signature / stale auth_date -> 401. Authenticated but not authorized
    -> 403. Success -> 303 redirect to POST_LOGIN_PATH with the cookie set."""
    params = dict(request.query_params)
    remember = params.pop("remember", "0") in ("1", "true", "True", "on")

    user = tg_auth.verify_login_widget(params, settings.login_bot_token)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid Telegram login")

    tg_id = user["id"]
    if not tg_auth.authorize(
        auth_conn, tg_id, APP_NAME, superuser_ids=settings.superuser_ids
    ):
        raise HTTPException(status_code=403, detail="not authorized for this app")

    token, max_age = tg_auth.issue_session(
        tg_id, secret=settings.session_secret, remember=remember
    )
    # 303 so the browser issues a GET to the post-login path after the redirect.
    response = RedirectResponse(url=POST_LOGIN_PATH, status_code=303)
    _set_session_cookie(response, token, max_age, settings)
    return response


@auth_router.post("/logout")
def logout(settings: AuthSettings = Depends(get_auth_settings)):
    """Clear the SSO cookie. Uses the SAME key + Domain so it actually clears the
    cross-subdomain cookie. Returns ok."""
    response = JSONResponse({"ok": True})
    response.delete_cookie(
        key=SESSION_COOKIE,
        domain=settings.cookie_domain,
        path="/",
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


# --- App factory ------------------------------------------------------------


def create_app() -> FastAPI:
    """Build the dashboard FastAPI app.

    Issue #5: every /api route (the read ``router`` and the action
    ``writer_router``) is gated by ``require_auth(app="jobhunter")``. The public
    login flow (``auth_router``: /login, /auth/callback, /logout) is NOT gated.
    """
    app = FastAPI(title="Job Hunter Dashboard API", version="1.0.0")
    app.include_router(auth_router)  # public login flow (NOT gated)
    app.include_router(router)  # read routes (issue #3), now gated
    app.include_router(writer_router)  # write/action routes (issue #4), now gated
    return app


app = create_app()
