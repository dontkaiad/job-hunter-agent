"""ORCHESTRATION: the advance() state-machine dispatcher (DESIGN.md §1).

advance() is the SOLE writer of work_items.state. It combines pure decisions
(states / extract / scoring) with store writes inside one transaction, and
drives the real LLM + FX integrations injected via a Deps bundle.

Transition kinds:
  - deterministic: T1 (extract), T2 (score), T3/T4 (reject/surface)
  - hitl:          T5..T9 (gate decisions), T12 (send), T13..T21 (post-send
                   response funnel: screening/interview/offer/decline/close)
  - agent(LLM):    T10 (research), T11 (draft)

Idempotency: advancing a terminal item is a no-op 'terminal' result.
HITL transitions without a legal decision return 'needs_human' and do not move.
"""

from __future__ import annotations

import json
import time

import psycopg
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import agents, geo, scoring, store, vacancy_fetch, vacancy_url
from .extract import extract as heuristic_extract
from .extract import reconcile as reconcile_extract
from .fx import FxRates
from .llm import (
    CHEAP_MODEL, JUDGE_MODEL, InsufficientContent, LLMClient, llm_extract, llm_score,
)
from .profile import Profile, example_profile
from .schema_extract import ExtractResult, from_dict, serialize
from .scoring import passes_threshold
from .states import (
    APPROVED, DRAFTED, EXTRACTED, KIND_AGENT, KIND_DETERMINISTIC, KIND_HITL,
    REJECTED, RESEARCHED, SCORED, SURFACED, DISCOVERED,
    allowed_transitions, is_terminal, transition_for_decision,
)
from .store import WorkItem

ACTOR_SYSTEM = "system"
ACTOR_HUMAN = "human"
ACTOR_AGENT = "agent"


# Account-wide LLM outages (no credits, bad key): every further call in the
# run would fail too, so scoring stops for the run instead of burning calls.
FAIL_FATAL = "credits/auth"
FAIL_TRANSIENT = "rate-limit/5xx/timeout"
FAIL_PARSE = "parse"


def classify_llm_error(exc: BaseException) -> str:
    """Bucket a scoring exception: FAIL_FATAL | FAIL_TRANSIENT | FAIL_PARSE.

    Matches by class name / status_code so this module never imports anthropic.
    """
    names = {c.__name__ for c in type(exc).__mro__}
    status = getattr(exc, "status_code", None)
    msg = str(exc).lower()
    if (
        names & {"AuthenticationError", "PermissionDeniedError"}
        or status in (401, 403)
        or "credit balance" in msg
    ):
        return FAIL_FATAL
    if (
        names & {"RateLimitError", "APIConnectionError", "APITimeoutError",
                 "InternalServerError", "TimeoutError", "ConnectionError"}
        or (isinstance(status, int) and (status == 429 or status >= 500))
    ):
        return FAIL_TRANSIENT
    return FAIL_PARSE


@dataclass
class ScoreRunStats:
    """Per-harvest scoring counters (feeds the ops alert) + the outage stop flag.

    ``halt_on_fatal`` is True only inside a harvest run; elsewhere (add-by-URL)
    a credits/auth error just defers the single item.
    """

    halt_on_fatal: bool = False
    attempted: int = 0
    failed: int = 0
    deferred: int = 0
    marked_failed: int = 0
    skipped_halted: int = 0
    halted: bool = False
    kinds: Dict[str, int] = field(default_factory=dict)
    # Parked, NOT failures: no attempt spent, never counted in failed/deferred.
    link_only: int = 0       # stub posts skipped before the LLM
    fetched: int = 0         # stub text successfully fetched from its link
    fetch_failed: int = 0    # fetch attempted and failed (retried, max N)
    denylisted: int = 0      # stub on a denylisted host: never fetched
    duplicates: int = 0      # same vacancy URL as an older row: dropped like a source_link dup
    insufficient: int = 0    # LLM said the posting has no content to judge
    insufficient_parked: int = 0  # already judged `insufficient`, input unchanged: NOT re-sent
    # extract (T1) failures: an API/infra error DEFERS the extraction, never heuristics
    extract_attempted: int = 0
    extract_deferred: Dict[str, int] = field(default_factory=dict)   # by error kind
    extract_failed_final: int = 0     # hit the 3-try cap: kept for manual review
    extract_skipped_halted: int = 0   # not tried: credits/auth outage earlier in the run
    heuristic_kept: int = 0           # cut by the score but extracted heuristically: kept, not deleted
    heuristic_purged: int = 0         # such vacancies expired after heuristic_keep_days
    # geo guard (only vacancies that cleared the score threshold are checked)
    geo_checked: int = 0
    geo_cut: int = 0                  # INELIGIBLE: rejected, kept 30 days, shown in the dashboard
    geo_unknown: int = 0              # UNKNOWN: surfaced as a normal card flagged "гео?"
    geo_purged: int = 0               # geo-cut vacancies expired after 30 days
    geo_failed: int = 0               # facts call failed: surfaced unchecked (never cut on an error)


@dataclass
class Deps:
    """Injected collaborators. Any may be None for deterministic-only runs.

    - llm_client: enables smart extract, rubric score, research, draft.
    - fx: FxRates for salary RUB conversion (prefilter + display).
    - use_llm_extract: when True and llm_client present, T1 uses the LLM
      (falling back to heuristics on failure). Otherwise heuristics only.
    - cheap_model / judge_model: per-step model routing ids. cheap_model
      (Haiku) is used for extraction/research/draft; judge_model (Sonnet) for
      the relevance score.
    """

    llm_client: Optional[LLMClient] = None
    fx: Optional[FxRates] = None
    use_llm_extract: bool = True
    cheap_model: str = CHEAP_MODEL
    judge_model: str = JUDGE_MODEL
    # Confidence corridor bounds (default: [50, 70]).
    # Haiku scores in this range trigger a full judge re-score; judge's score
    # becomes final. Overridable via Config.score_corridor_lo/hi → env vars.
    corridor_lo: int = 40
    corridor_hi: int = 60
    surface_threshold: int = 50
    # Score prompt version (llm.SCORE_PROMPT_VERSIONS). Config default is v2 (live); this dataclass
    # default stays v1 so a bare Deps() is the old behaviour. Roll back with SCORE_PROMPT_VERSION=v1.
    score_prompt_version: str = "v1"
    # Geo guard: on only when explicitly enabled AND the private profile carries eligibility
    # constraints (profile.eligibility). Off by default so tests / the public example are inert.
    geo_guard: bool = False
    # Candidate profile (loaded from config/profile.*.yaml). Drives the rubric
    # profile block, the draft gender/honesty/signature, and the salary floor.
    # Defaults to the GENERIC example profile so deterministic-only / test runs
    # work without personal data; live runs inject the loaded local profile.
    profile: Profile = field(default_factory=example_profile)
    # Minimum score to persist a vacancy in work_items permanently.
    # Items scoring below this threshold are DELETED from work_items after
    # scoring — they never reach a terminal 'rejected' row. 0 = disabled.
    min_persist_score: int = 0
    # Failed scoring attempts before an item is marked score_failed (kept, not deleted).
    score_max_attempts: int = 3
    extract_max_attempts: int = 3
    # Posts with fewer substantive chars than this are link-only stubs (no LLM).
    link_only_min_chars: int = scoring.DEFAULT_LINK_ONLY_MIN_CHARS
    # Shadow sample of below-the-cut deletions (eval_shadow). Off unless configured.
    shadow_rate_percent: int = 0
    shadow_max_rows: int = 200
    shadow_ttl_days: int = 90
    heuristic_keep_days: int = 30
    # Fetch the real page behind link-only/insufficient posts before scoring.
    # Off by default (no network in tests); live runs enable it via build_deps.
    fetch_enabled: bool = False
    fetch_denylist: Tuple[str, ...] = vacancy_fetch.DEFAULT_DENYLIST
    fetch_max_attempts: int = 3
    fetch_pause_s: float = 0.0
    fetch_fn: Optional[Callable[..., vacancy_fetch.FetchResult]] = None
    score_stats: ScoreRunStats = field(default_factory=ScoreRunStats)
    # T1 AI/ML/LLM topic gate (scoring.topic_prefilter). False (default):
    # DRY-RUN — a miss is only logged (`[topic-gate]`), extract still runs
    # normally. True: a miss is treated like the other T1 prefilter drops
    # (heuristic extract only, no LLM call). Keep False until a few days of
    # dry-run logs confirm the false-positive rate on real traffic is low.
    topic_gate_enforce: bool = False


@dataclass
class AdvanceResult:
    status: str                 # 'moved' | 'needs_human' | 'terminal' | 'noop'
    item_id: int
    from_state: str
    to_state: Optional[str] = None
    transition: Optional[str] = None  # T-name
    reason: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


# --- Helpers ----------------------------------------------------------------


def _load_extracted(item: WorkItem) -> Optional[ExtractResult]:
    if not item.extracted_json:
        return None
    try:
        return from_dict(json.loads(item.extracted_json))
    except Exception:
        return None


def _salary_max_rub(extracted: ExtractResult, deps: Deps) -> Optional[float]:
    """Convert the top of the salary range to RUB, if possible."""
    if extracted.salary_max is None and extracted.salary_min is None:
        return None
    top = extracted.salary_max if extracted.salary_max is not None else extracted.salary_min
    if extracted.currency is None:
        return None
    if extracted.currency.upper() == "RUB":
        return float(top)
    if deps is None or deps.fx is None:
        return None
    try:
        return deps.fx.convert(top, extracted.currency)
    except Exception:
        return None


def _salary_floor_rub(deps: Deps) -> Optional[float]:
    """Convert the candidate's EUR/month-gross salary floor to RUB via FX.

    The floor value comes from the loaded profile (``deps.profile.
    salary_floor_eur``), NOT a hardcoded constant. Mirrors ``_salary_max_rub``:
    both the salary top and this floor are taken to a common currency (RUB) so
    the deterministic guard in scoring.py can compare them regardless of the
    posting's currency. Returns None when FX is unavailable, in which case the
    guard simply cannot fire.
    """
    if deps is None or deps.fx is None:
        return None
    try:
        return deps.fx.convert(deps.profile.salary_floor_eur, "EUR")
    except Exception:
        return None


# --- Per-transition handlers ------------------------------------------------


def _defer_extract(
    conn: psycopg.Connection, item: WorkItem, deps: Deps, exc: BaseException
) -> AdvanceResult:
    """Extraction failed: the item stays 'discovered' and is retried next harvest.

    credits/auth stop extract AND scoring for the rest of the run and burn no attempt;
    anything else counts one of ``extract_max_attempts`` (then extract_failed: kept for
    manual review, never scored, never deleted)."""
    stats = deps.score_stats
    kind = classify_llm_error(exc)
    fatal = kind == FAIL_FATAL
    stats.extract_deferred[kind] = stats.extract_deferred.get(kind, 0) + 1
    if fatal and stats.halt_on_fatal:
        stats.halted = True
    attempts, failed = store.record_extract_failure(
        conn, item.id, max_attempts=deps.extract_max_attempts,
        reason=f"extract deferred ({kind}): {type(exc).__name__}", count_attempt=not fatal,
    )
    if failed:
        stats.extract_failed_final += 1
    print(
        f"[extract] id={item.id} deferred ({kind}) attempts={attempts}"
        f"{' -> extract_failed' if failed else ''}: {type(exc).__name__}",
        flush=True,
    )
    return AdvanceResult(
        "noop", item.id, item.state, reason=f"extract deferred ({kind})",
        extra={"extract_failed": failed, "attempts": attempts},
    )


def _do_extract(conn: psycopg.Connection, item: WorkItem, deps: Deps) -> AdvanceResult:
    """T1: discovered -> extracted (LLM smart extract; heuristics ONLY by design).

    Cost-aware routing: a cheap deterministic pre-filter runs over raw_text
    FIRST, before any LLM call. High-volume noisy sources (HN Who's Hiring
    ingests every top-level comment with zero content filtering; WWR/Jobicy
    can mirror the same posting under different source_channels) would
    otherwise send every ingested item straight to a paid Haiku extract call.
    Two deterministic drops skip that call (heuristic extract only, same as
    the "no LLM configured" path):
      1) ``scoring.text_prefilter`` — obvious junk (empty/too-short text,
         "looking for work" posts). LENIENT: only unambiguous non-jobs drop.
      2) cross-source duplicate — another row already carries the SAME
         source_link (the partial unique index only dedups within one
         source_channel, so a job mirrored across sources isn't caught there).
      3) ``scoring.topic_prefilter`` — AI/ML/LLM keyword gate over title + a
         raw-text prefix. Confirmed data (2026-07-24): 46/48 recent rejects
         were score<T, not location/salary — the rubric IS correctly rejecting
         generic (non-AI) postings from WWR/Jobicy, we're just paying Haiku to
         find that out every time. DRY-RUN by default (Deps.topic_gate_enforce
         = False): a miss is only logged, never drops the item — this gate is
         intentionally lenient and a keyword miss on a real AI role (e.g. one
         titled plain "Backend Engineer") is a live risk, so it needs a few
         days of `[topic-gate]` log review before it's allowed to cut anything.
    """
    source_channel = item.source_channel or ""
    raw = item.raw_text or ""
    extracted: ExtractResult
    used = "heuristic"

    pf = scoring.text_prefilter(raw)
    dup_id = (
        store.find_duplicate_by_source_link(conn, item.source_link, item.id)
        if pf.keep else None
    )
    topic_pf = (
        scoring.topic_prefilter(raw)
        if pf.keep and dup_id is None else scoring.PrefilterResult(True, None)
    )
    if pf.keep and dup_id is None and not topic_pf.keep:
        print(
            f"[topic-gate] id={item.id} source={source_channel!r}"
            f" would-filter: {topic_pf.reason}"
            f" (enforce={deps.topic_gate_enforce})",
            flush=True,
        )
    topic_drop = deps.topic_gate_enforce and not topic_pf.keep
    use_llm = (
        deps.use_llm_extract and deps.llm_client is not None
        and pf.keep and dup_id is None and not topic_drop
    )

    mode = "heuristic:no-llm"
    if use_llm:
        if item.extract_failed:
            return AdvanceResult("noop", item.id, item.state, reason="extract_failed: manual review")
        if deps.score_stats.halted:
            # Credits/auth outage earlier in this run: every call would fail too.
            deps.score_stats.extract_skipped_halted += 1
            return AdvanceResult("noop", item.id, item.state, reason="extract halted (credits/auth)")
        deps.score_stats.extract_attempted += 1
        try:
            extracted = llm_extract(
                deps.llm_client, raw, source_channel, item.source_link,
                model=deps.cheap_model,
            )
            # INTEGRATION FIX: the LLM extract alone misses deterministic signals
            # (hashtag remote/seniority, "Компания: X", benefits, bottom contact).
            # Run a deterministic RECONCILE pass over the SAME raw_text so those
            # fields always reach the ExtractResult, even when Haiku omits them.
            # Precedence per field is documented in extract.reconcile.
            extracted = reconcile_extract(
                extracted, raw, source_channel, item.source_link
            )
            used, mode = "llm+reconcile", "llm"
        except Exception as exc:  # noqa: BLE001
            # NEVER fall back to heuristics on a failure: an API/infra error (or an
            # unparseable reply) must not silently lower the quality of the input that
            # scoring - and the below-the-cut deletion - then relies on. Defer instead.
            return _defer_extract(conn, item, deps, exc)
    else:
        extracted = heuristic_extract(raw, source_channel, item.source_link)
        if not pf.keep:
            used, mode = f"heuristic(prefilter:{pf.reason})", "heuristic:prefilter"
        elif dup_id is not None:
            used, mode = f"heuristic(duplicate of #{dup_id})", "heuristic:duplicate"
        elif topic_drop:
            used, mode = f"heuristic(topic-filtered:{topic_pf.reason})", "heuristic:topic"

    extracted.source_channel = source_channel
    if item.source_link:
        extracted.source_link = item.source_link

    store.update_state(
        conn, item.id, EXTRACTED,
        from_state=item.state, kind=KIND_DETERMINISTIC, actor=ACTOR_SYSTEM,
        reason=f"extract ({used})", extracted_json=serialize(extracted),
        extract_mode=mode,
    )
    return AdvanceResult("moved", item.id, item.state, EXTRACTED, "T1", f"extract:{used}")


# Reserved key in extracted_json holding the Sonnet rationale (transparency
# surface shown on the card). Stored alongside the §4 schema fields.
REASONING_KEY = "Обоснование"


def _store_reasoning(extracted_json: str, reasoning: str) -> str:
    """Merge the Sonnet rationale into the extracted_json blob (reserved key)."""
    return agents.merge_aux(extracted_json, REASONING_KEY, reasoning)


def _maybe_fetch(
    conn: psycopg.Connection, item: WorkItem, deps: Deps, raw: str
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Fetch the stub's link once per harvest. Returns (text|None, fetch_status,
    final_url|None).

    Denylisted / no-link stubs are settled WITHOUT a request; real failures are
    retried up to ``fetch_max_attempts`` harvests. NEVER raises, never scores.
    """
    stats = deps.score_stats
    st = item.fetch_status
    if st == vacancy_fetch.DENYLISTED:
        stats.denylisted += 1
        return None, st, None
    if st == vacancy_fetch.NO_URL:
        return None, st, None
    if item.fetch_attempts >= deps.fetch_max_attempts:
        return None, st, None
    fn = deps.fetch_fn or vacancy_fetch.fetch_vacancy_text
    res = fn(raw, item.source_link, denylist=deps.fetch_denylist)
    if res.status not in (vacancy_fetch.DENYLISTED, vacancy_fetch.NO_URL) and deps.fetch_pause_s:
        time.sleep(deps.fetch_pause_s)
    if res.status == vacancy_fetch.OK:
        store.record_fetch(conn, item.id, res.status, text=res.text, url=res.url)
        stats.fetched += 1
        print(f"[fetch] id={item.id} ok host={res.host} chars={len(res.text)}", flush=True)
        return res.text, res.status, res.url
    if res.status == vacancy_fetch.DENYLISTED:
        store.record_fetch(conn, item.id, res.status, url=res.url)
        stats.denylisted += 1
    elif res.status == vacancy_fetch.NO_URL:
        store.record_fetch(conn, item.id, res.status)
    else:
        n = store.record_fetch(conn, item.id, vacancy_fetch.FAILED, url=res.url, count_attempt=True)
        stats.fetch_failed += 1
        print(f"[fetch] id={item.id} failed host={res.host} attempts={n}", flush=True)
    return None, res.status, None


def _park_resume(item: WorkItem, deps: Deps) -> AdvanceResult:
    """An `insufficient` vacancy whose input did not change since the model judged it:
    no model call, no DB write, no attempt."""
    deps.score_stats.insufficient_parked += 1
    print(f"[score] id={item.id} insufficient, input unchanged -> not re-scored", flush=True)
    return AdvanceResult(
        "noop", item.id, item.state, reason="score parked (insufficient, input unchanged)",
        extra={"defer_reason": "insufficient", "rescored": False},
    )


def _park_item(
    conn: psycopg.Connection, item: WorkItem, deps: Deps, reason: str, detail: str = "",
    input_hash: Optional[str] = None,
) -> AdvanceResult:
    """Park an item that cannot be scored for lack of content. NOT a failure:
    no attempt spent, no score written, no deletion; it stays in EXTRACTED."""
    stats = deps.score_stats
    if reason == "link_only":
        stats.link_only += 1
    else:
        stats.insufficient += 1
    store.set_defer_reason(
        conn, item.id, reason,
        audit_reason=f"score parked ({reason})" + (f": {detail}" if detail else ""),
        input_hash=input_hash,
    )
    print(f"[score] id={item.id} parked ({reason})", flush=True)
    return AdvanceResult(
        "noop", item.id, item.state, reason=f"score parked ({reason})",
        extra={"defer_reason": reason},
    )


def _defer_scoring(
    conn: psycopg.Connection, item: WorkItem, deps: Deps, exc: BaseException
) -> AdvanceResult:
    """Scoring failed: keep the item in EXTRACTED for the next run's retry."""
    stats = deps.score_stats
    kind = classify_llm_error(exc)
    fatal = kind == FAIL_FATAL
    stats.failed += 1
    stats.deferred += 1
    stats.kinds[kind] = stats.kinds.get(kind, 0) + 1
    if fatal and stats.halt_on_fatal:
        stats.halted = True
    # A credits/auth outage is not the vacancy's fault: don't burn its attempts.
    attempts, failed = store.record_score_failure(
        conn, item.id, max_attempts=deps.score_max_attempts,
        reason=f"score deferred ({kind}): {type(exc).__name__}",
        count_attempt=not fatal,
    )
    if failed:
        stats.marked_failed += 1
    print(
        f"[score] id={item.id} haiku=err deferred ({kind}) attempts={attempts}"
        f"{' -> score_failed' if failed else ''}: {type(exc).__name__}",
        flush=True,
    )
    return AdvanceResult(
        "noop", item.id, item.state, reason=f"score deferred ({kind})",
        extra={"score_failed": failed, "attempts": attempts},
    )


def _do_score(conn: psycopg.Connection, item: WorkItem, deps: Deps) -> AdvanceResult:
    """T2: extracted -> scored.

    Cost-aware routing:
      1) LENIENT deterministic prefilter drops obvious non-jobs (score 0,
         no LLM call for junk).
      2) Haiku scores ALL surviving vacancies (cheap first pass).
      3) Confidence routing on the Haiku score:
         - In [corridor_lo, corridor_hi]: judge re-scores (full llm_score call);
           judge's score becomes FINAL. Catches uncertain near-threshold cases.
         - Above corridor_hi (confident surface): Haiku score AND text both
           final, no judge call (#303: a judge "refine" pass used to run here
           just to reword the Обоснование — removed, it burned the expensive
           model on every confident accept for a cosmetic-only text pass).
         - Below corridor_lo (confident reject): Haiku score final, no second
           call.
    """
    extracted = _load_extracted(item)
    if extracted is None:
        return AdvanceResult("noop", item.id, item.state, reason="missing extracted_json")

    if item.score_failed:
        return AdvanceResult("noop", item.id, item.state, reason="score_failed: manual review")

    raw = item.raw_text or ""
    pf = scoring.prefilter(extracted, raw)
    dup_id = (
        store.find_duplicate_by_source_link(conn, item.source_link, item.id)
        if pf.keep else None
    )
    dup_by = "source_link"
    if pf.keep and dup_id is None:
        # Same vacancy posted again (other channel / source): its own post link
        # differs, but the vacancy link in the body is the same. Checked BEFORE any
        # fetch or LLM call so a duplicate costs nothing.
        pkey = item.primary_url_key
        if pkey is None:
            pkey = vacancy_url.primary_url_key(raw, item.source_link, item.source_channel)
            if pkey:
                store.set_url_keys(conn, item.id, primary=pkey)
        dup_id = store.find_duplicate_by_url_keys(
            conn, [pkey, item.final_url_key], item.id
        )
        dup_by = "vacancy URL"
    fetched_text = item.fetched_text
    fetch_status = item.fetch_status
    if (
        pf.keep and dup_id is None and deps.fetch_enabled and not fetched_text
        and deps.llm_client is not None and not deps.score_stats.halted
        and (
            item.defer_reason in ("link_only", "insufficient")
            or scoring.is_link_only(raw, deps.link_only_min_chars)
        )
    ):
        fetched_text, fetch_status, final_url = _maybe_fetch(conn, item, deps, raw)
        fkey = vacancy_url.canonical_url(final_url)
        if fkey and fkey != item.final_url_key:
            # A short / redirecting link can resolve to a vacancy we already have.
            store.set_url_keys(conn, item.id, final=fkey)
            dup_id = store.find_duplicate_by_url_keys(conn, [fkey], item.id)
            dup_by = "vacancy URL"
    if pf.keep and dup_id is None and item.defer_reason == "insufficient":
        # The model already said "nothing to judge" for exactly this input (post text +
        # fetched page). Asking again costs money and cannot change the answer: wait for
        # the input to change (e.g. the fetch just brought the page).
        if item.defer_input_hash == scoring.input_hash(raw, fetched_text):
            return _park_resume(item, deps)
    if (
        pf.keep and dup_id is None and scoring.is_link_only(raw, deps.link_only_min_chars)
        and len(fetched_text or "") < deps.link_only_min_chars
    ):
        # Stub post (just a link) and no page text: nothing to judge, so no LLM
        # call and no attempt.
        return _park_item(conn, item, deps, "link_only", f"fetch={fetch_status}" if fetch_status else "")
    if not pf.keep or dup_id is not None:
        score = 0
        reasoning = (
            f"prefilter: {pf.reason}" if not pf.keep
            else f"prefilter: duplicate of #{dup_id} (same {dup_by})"
        )
        if pf.keep and dup_by == "vacancy URL":
            deps.score_stats.duplicates += 1
        used = "prefilter-drop"
        print(f"[score] id={item.id} haiku=0 final=0 (prefilter)", flush=True)
    else:
        reasoning = ""
        used = "haiku"
        if deps.llm_client is not None:
            if deps.score_stats.halted:
                # Credits/auth outage earlier in this run: every call would fail.
                deps.score_stats.skipped_halted += 1
                return AdvanceResult(
                    "noop", item.id, item.state, reason="scoring halted (credits/auth)"
                )
            try:
                # First pass: Haiku scores every vacancy (fast + cheap).
                deps.score_stats.attempted += 1
                verdict = llm_score(
                    deps.llm_client, extracted, raw, model=deps.cheap_model,
                    profile=deps.profile,
                    fetched_text=fetched_text,
                    prompt_version=deps.score_prompt_version,
                )
                haiku_score = scoring.clamp_score(verdict["score"])
                haiku_reasoning = verdict.get("reasoning", "")

                in_corridor = deps.corridor_lo <= haiku_score <= deps.corridor_hi

                if in_corridor:
                    # Uncertainty zone: judge re-scores; judge's score is final.
                    try:
                        judge_verdict = llm_score(
                            deps.llm_client, extracted, raw,
                            model=deps.judge_model, profile=deps.profile,
                            fetched_text=fetched_text,
                            prompt_version=deps.score_prompt_version,
                        )
                        score = scoring.clamp_score(judge_verdict["score"])
                        reasoning = judge_verdict.get("reasoning", "")
                        used = "haiku+corridor-judge"
                        print(
                            f"[score] id={item.id} haiku={haiku_score}"
                            f" → corridor → judge={score} final={score}",
                            flush=True,
                        )
                    except Exception as exc:  # noqa: BLE001
                        score = haiku_score
                        reasoning = haiku_reasoning
                        used = "haiku+corridor-judge(err)"
                        print(
                            f"[score] id={item.id} haiku={haiku_score}"
                            f" → corridor → judge FAILED ({exc!r}) fallback={score}",
                            flush=True,
                        )

                elif haiku_score >= deps.surface_threshold:
                    # Above corridor: confident surface. Haiku score AND text both
                    # final — no Sonnet call. (#303: Sonnet used to "refine" the
                    # Обоснование here even though the score was already decided;
                    # Kai flagged it as burning the expensive model on cosmetic
                    # text polish on every single confident-accept item, not just
                    # the genuinely uncertain corridor cases. Removed.)
                    score = haiku_score
                    reasoning = haiku_reasoning
                    used = "haiku"
                    print(
                        f"[score] id={item.id} haiku={score} final={score} (no corridor)",
                        flush=True,
                    )

                else:
                    # Below corridor: confident reject, Haiku score final.
                    score = haiku_score
                    reasoning = haiku_reasoning
                    used = "haiku"
                    print(
                        f"[score] id={item.id} haiku={score} final={score} (no corridor)",
                        flush=True,
                    )

            except InsufficientContent as exc:
                # Not a failure and not a zero: park it, keep it, spend no attempt.
                return _park_item(conn, item, deps, "insufficient", str(exc)[:200],
                                  input_hash=scoring.input_hash(raw, fetched_text))
            except Exception as exc:  # noqa: BLE001 - degrade, never crash T2
                # Unknown score != zero score: defer, never fabricate score=0
                # (a 0 below min_persist_score gets the vacancy DELETED).
                return _defer_scoring(conn, item, deps, exc)
        else:
            # No judge available: surface for manual review rather than silent reject.
            score = deps.surface_threshold
            reasoning = "no LLM judge configured; surfaced for manual review"
            used = "no-llm"
            print(f"[score] id={item.id} final={score} (no-llm)", flush=True)

    extracted.relevance_score = float(score)
    extracted.reasons = [reasoning] if reasoning else []
    blob = _store_reasoning(serialize(extracted), reasoning)

    store.update_state(
        conn, item.id, SCORED,
        from_state=item.state, kind=KIND_DETERMINISTIC, actor=ACTOR_SYSTEM,
        reason=f"score={score} ({used})",
        extracted_json=blob, relevance_score=float(score),
    )
    return AdvanceResult(
        "moved", item.id, item.state, SCORED, "T2", f"score={score}",
        extra={"score": score, "reasoning": reasoning, "prefilter_keep": pf.keep},
    )


def _is_prefilter_drop(item: WorkItem) -> bool:
    """True when the score 0 came from a deterministic prefilter (junk / duplicate),
    not from the model: those drops say nothing about the scorer, so they are not
    sampled for the shadow set."""
    try:
        note = json.loads(item.extracted_json or "{}").get(REASONING_KEY) or ""
    except Exception:
        return False
    return str(note).startswith("prefilter:")


def _maybe_shadow_copy(conn: psycopg.Connection, item: WorkItem, deps: Deps, score,
                       kind: str = "score", rate_percent: Optional[int] = None) -> None:
    """Best-effort: keep a copy of a deterministic share of model-scored deletions (kind
    'score') or of every topic-gated cut (kind 'topic', 100%, own row cap).
    A failure here must never block what the caller does next."""
    if not deps.shadow_rate_percent or _is_prefilter_drop(item):
        return
    try:
        if store.shadow_copy(
            conn, item, score,
            rate_percent=deps.shadow_rate_percent if rate_percent is None else rate_percent,
            max_rows=deps.shadow_max_rows, ttl_days=deps.shadow_ttl_days, kind=kind,
        ):
            print(f"[shadow] id={item.id} score={score} copied to eval_shadow ({kind})", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[shadow] id={item.id} copy failed (deletion continues): {exc!r}", flush=True)


def _geo_check(conn: psycopg.Connection, item: WorkItem, deps: Deps) -> Optional[str]:
    """Geo guard for a vacancy that cleared the threshold. Returns the cut reason when INELIGIBLE,
    else None (the item is surfaced; UNKNOWN is recorded so the card carries the "гео?" flag).

    Facts come from one Haiku call (geo.llm_geo_facts, facts only); the verdict is the pure
    geo.decide() against the candidate's private eligibility constraints. A failed call never
    cuts: the item is surfaced unchecked and counted in geo_failed."""
    if deps is None or not deps.geo_guard or deps.llm_client is None:
        return None
    elig = getattr(deps.profile, "eligibility", None)
    if elig is None or not getattr(elig, "home_markets", None):
        return None
    if item.geo_verdict is not None:        # already judged (re-advance): reuse
        return item.geo_reason if item.geo_verdict == geo.INELIGIBLE.lower() else None
    stats = deps.score_stats
    stats.geo_checked += 1
    try:
        facts = geo.llm_geo_facts(deps.llm_client, item.raw_text or "", item.fetched_text, model=deps.cheap_model)
    except Exception as exc:  # noqa: BLE001 - a status check must never cut or crash on an error
        stats.geo_failed += 1
        print(f"[geo] id={item.id} facts failed, surfaced unchecked: {exc!r}", flush=True)
        return None
    verdict, reason = geo.decide(facts, elig)
    store.set_geo(conn, item.id, verdict.lower(), reason, json.dumps(facts.to_dict(), ensure_ascii=False))
    if verdict == geo.INELIGIBLE:
        stats.geo_cut += 1
        print(f"[geo] id={item.id} cut: {reason}", flush=True)
        return reason
    if verdict == geo.UNKNOWN:
        stats.geo_unknown += 1
    return None


def _do_reject_or_surface(conn: psycopg.Connection, item: WorkItem, deps: Deps) -> AdvanceResult:
    """T3/T4: scored -> rejected | surfaced.

    Deterministic only:
      - HARD salary guard wins: if the RUB-equivalent salary top is known and
        below the floor, reject regardless of the (Sonnet) relevance score.
      - HARD location guard wins: office-only with no relocation/visa signal
        is physically unavailable to the candidate, reject regardless of how
        well the stack matches.
      - Otherwise the surface threshold T is applied to the stored score.
    """
    extracted = _load_extracted(item)
    if extracted is None:
        return AdvanceResult("noop", item.id, item.state, reason="missing extracted_json")

    score = extracted.relevance_score if extracted.relevance_score is not None else 0
    min_persist = deps.min_persist_score if deps is not None else 0
    # Only drop on a score the model actually produced (never on a NULL score).
    if min_persist > 0 and extracted.relevance_score is not None and score < min_persist:
        if item.extract_mode in store.PROTECTED_EXTRACT_MODES:
            # The score was computed from heuristic fields the model never saw (topic
            # gate / legacy fallback): do NOT delete. Keep it as rejected for
            # heuristic_keep_days and copy it to the shadow set to measure the gate.
            _maybe_shadow_copy(conn, item, deps, score, kind="topic", rate_percent=100)
            deps.score_stats.heuristic_kept += 1
            store.update_state(
                conn, item.id, REJECTED,
                from_state=item.state, kind=KIND_DETERMINISTIC, actor=ACTOR_SYSTEM,
                reason=(f"score {scoring.clamp_score(score)} < {min_persist}: kept "
                        f"{deps.heuristic_keep_days}d (extract_mode={item.extract_mode})"),
            )
            print(f"[harvest] kept id={item.id} score={score} (heuristic extract, not deleted)", flush=True)
            return AdvanceResult(
                "moved", item.id, item.state, REJECTED, "T3", "below cut, kept (heuristic extract)",
                extra={"score": scoring.clamp_score(score)},
            )
        _maybe_shadow_copy(conn, item, deps, score)
        store.delete_item(conn, item.id)
        print(
            f"[harvest] dropped id={item.id} url={item.source_link!r} "
            f"score={score} < {min_persist}",
            flush=True,
        )
        return AdvanceResult(
            "noop", item.id, item.state,
            reason=f"dropped: score {score} < min_persist {min_persist}",
        )

    salary_rub = _salary_max_rub(extracted, deps)
    floor_rub = _salary_floor_rub(deps)
    if scoring.salary_guard_reject(salary_rub, floor_rub):
        store.update_state(
            conn, item.id, REJECTED,
            from_state=item.state, kind=KIND_DETERMINISTIC, actor=ACTOR_SYSTEM,
            reason=f"hard reject: salary below EUR {deps.profile.salary_floor_eur:.0f}/mo floor",
        )
        return AdvanceResult(
            "moved", item.id, item.state, REJECTED, "T3", "hard reject (salary)",
            extra={"salary_rub": salary_rub, "floor_rub": floor_rub},
        )

    if scoring.location_guard_reject(extracted.remote, extracted.relocation):
        store.update_state(
            conn, item.id, REJECTED,
            from_state=item.state, kind=KIND_DETERMINISTIC, actor=ACTOR_SYSTEM,
            reason="hard reject: оффлайн без релокации/визы — физически недоступно",
        )
        return AdvanceResult(
            "moved", item.id, item.state, REJECTED, "T3", "hard reject (location)",
            extra={"remote": extracted.remote, "relocation": extracted.relocation},
        )

    if scoring.remote_restriction_reject(
        extracted.remote, extracted.relocation, extracted.remote_location_restricted
    ):
        store.update_state(
            conn, item.id, REJECTED,
            from_state=item.state, kind=KIND_DETERMINISTIC, actor=ACTOR_SYSTEM,
            reason="hard reject: удалёнка ограничена резидентством без релокации/визы — физически недоступно",
        )
        return AdvanceResult(
            "moved", item.id, item.state, REJECTED, "T3", "hard reject (remote geo-restricted)",
            extra={
                "remote": extracted.remote,
                "relocation": extracted.relocation,
                "remote_location_restricted": extracted.remote_location_restricted,
            },
        )

    score = extracted.relevance_score if extracted.relevance_score is not None else 0
    if passes_threshold(score, deps.surface_threshold if deps is not None else None):
        cut = _geo_check(conn, item, deps)
        if cut is not None:
            store.update_state(
                conn, item.id, REJECTED,
                from_state=item.state, kind=KIND_DETERMINISTIC, actor=ACTOR_SYSTEM,
                reason=f"geo cut: {cut}",
            )
            return AdvanceResult(
                "moved", item.id, item.state, REJECTED, "T3", "geo cut",
                extra={"score": scoring.clamp_score(score), "geo_reason": cut},
            )
        store.update_state(
            conn, item.id, SURFACED,
            from_state=item.state, kind=KIND_DETERMINISTIC, actor=ACTOR_SYSTEM,
            reason=f"score {scoring.clamp_score(score)} >= T",
        )
        return AdvanceResult(
            "moved", item.id, item.state, SURFACED, "T4", "above threshold",
            extra={"score": scoring.clamp_score(score)},
        )

    store.update_state(
        conn, item.id, REJECTED,
        from_state=item.state, kind=KIND_DETERMINISTIC, actor=ACTOR_SYSTEM,
        reason=f"score {scoring.clamp_score(score)} < T",
    )
    return AdvanceResult(
        "moved", item.id, item.state, REJECTED, "T3", "below threshold",
        extra={"score": scoring.clamp_score(score)},
    )


def _do_research(conn: psycopg.Connection, item: WorkItem, deps: Deps) -> AdvanceResult:
    """T10: approved -> researched (agent/LLM)."""
    extracted = _load_extracted(item)
    if extracted is None:
        return AdvanceResult("noop", item.id, item.state, reason="missing extracted_json")
    if deps.llm_client is None:
        return AdvanceResult("needs_human", item.id, item.state, reason="no LLM client for research")

    research_data = agents.research(
        deps.llm_client, extracted, item.raw_text or "", model=deps.cheap_model
    )
    merged = agents.merge_aux(item.extracted_json, "research", research_data)
    store.update_state(
        conn, item.id, RESEARCHED,
        from_state=item.state, kind=KIND_AGENT, actor=ACTOR_AGENT,
        reason="research complete", extracted_json=merged,
    )
    return AdvanceResult("moved", item.id, item.state, RESEARCHED, "T10", "researched",
                         extra={"research": research_data})


def _do_draft(conn: psycopg.Connection, item: WorkItem, deps: Deps) -> AdvanceResult:
    """T11: researched -> drafted (agent/LLM)."""
    extracted = _load_extracted(item)
    if extracted is None:
        return AdvanceResult("noop", item.id, item.state, reason="missing extracted_json")
    if deps.llm_client is None:
        return AdvanceResult("needs_human", item.id, item.state, reason="no LLM client for draft")

    research_data = None
    try:
        existing = json.loads(item.extracted_json or "{}")
        research_data = existing.get("research")
    except Exception:
        pass

    draft_text = agents.draft(
        deps.llm_client, extracted, item.raw_text or "", research_data,
        model=deps.cheap_model, profile=deps.profile,
    )
    merged = agents.merge_aux(item.extracted_json, "draft", draft_text)
    store.update_state(
        conn, item.id, DRAFTED,
        from_state=item.state, kind=KIND_AGENT, actor=ACTOR_AGENT,
        reason="draft generated", extracted_json=merged,
    )
    return AdvanceResult("moved", item.id, item.state, DRAFTED, "T11", "drafted",
                         extra={"draft": draft_text})


# Map current state -> deterministic/agent handler (HITL handled separately).
# SENT is intentionally absent: the post-send funnel (T13..T21) is ALL manual,
# so a SENT item waits for a human decision (advance() returns 'needs_human').
_AUTO_HANDLERS = {
    DISCOVERED: _do_extract,
    EXTRACTED: _do_score,
    SCORED: _do_reject_or_surface,
    APPROVED: _do_research,
    RESEARCHED: _do_draft,
}


# --- The dispatcher ---------------------------------------------------------


def advance(
    conn: psycopg.Connection,
    item: WorkItem,
    decision: Optional[str] = None,
    deps: Optional[Deps] = None,
    reason: Optional[str] = None,
    channel: Optional[str] = None,
    actor_id: Optional[str] = None,
) -> AdvanceResult:
    """Single dispatcher for ALL transitions (DESIGN.md §1.5).

    Args:
        conn: open sqlite connection.
        item: the work item to advance (as last read).
        decision: a human decision (states.DECISION_*) for HITL gates.
        deps: injected LLM/FX collaborators.
        reason: optional human-supplied note recorded with HITL moves.
        channel / actor_id: provenance of a HITL decision (dashboard | bot | api |
            script) and the Telegram id behind it; stored on the transition row.
    """
    deps = deps or Deps()
    try:
        return _advance(conn, item, decision, deps, reason, channel, actor_id)
    except store.StateConflict as exc:
        # Another actor (a human in the bot / dashboard, or a parallel run) moved the item first.
        # Their move stands; this one is dropped, not retried and not forced.
        print(f"[advance] id={item.id} {exc}; the earlier move stands", flush=True)
        return AdvanceResult("noop", item.id, exc.actual or item.state,
                             reason=f"state changed concurrently ({exc.expected} -> {exc.actual})",
                             extra={"conflict": True})


def _advance(
    conn: psycopg.Connection,
    item: WorkItem,
    decision: Optional[str],
    deps: Deps,
    reason: Optional[str],
    channel: Optional[str],
    actor_id: Optional[str],
) -> AdvanceResult:
    # 7) Idempotency: terminal items never move.
    if is_terminal(item.state):
        return AdvanceResult("terminal", item.id, item.state, reason="terminal state")

    outgoing = allowed_transitions(item.state)
    if not outgoing:
        return AdvanceResult("noop", item.id, item.state, reason="no outgoing transitions")

    # 4) HITL: if a decision was supplied, validate + apply it.
    if decision is not None:
        t = transition_for_decision(item.state, decision)
        if t is None:
            return AdvanceResult(
                "noop", item.id, item.state,
                reason=f"decision '{decision}' illegal from {item.state}",
            )
        store.update_state(
            conn, item.id, t.to_state,
            from_state=item.state, kind=KIND_HITL, actor=ACTOR_HUMAN,
            reason=reason or f"human:{decision}",
            channel=channel, actor_id=actor_id,
        )
        return AdvanceResult("moved", item.id, item.state, t.to_state, t.name,
                             reason or decision)

    # No decision supplied: run the automatic handler for this state, if any.
    handler = _AUTO_HANDLERS.get(item.state)
    if handler is not None:
        return handler(conn, item, deps)

    # 4) Otherwise this state only has HITL exits and none was supplied.
    hitl = [t for t in outgoing if t.kind == KIND_HITL]
    if hitl:
        return AdvanceResult(
            "needs_human", item.id, item.state,
            reason="human decision required",
            extra={"options": [t.decision for t in hitl]},
        )

    return AdvanceResult("noop", item.id, item.state, reason="nothing to do")


def advance_by_id(
    conn: psycopg.Connection,
    item_id: int,
    decision: Optional[str] = None,
    deps: Optional[Deps] = None,
    reason: Optional[str] = None,
    channel: Optional[str] = None,
    actor_id: Optional[str] = None,
) -> AdvanceResult:
    """Re-read the item by id (fresh state) then advance it."""
    item = store.get_item(conn, item_id)
    if item is None:
        return AdvanceResult("noop", item_id, "?", reason="item not found")
    return advance(conn, item, decision=decision, deps=deps, reason=reason,
                   channel=channel, actor_id=actor_id)


def run_to_gate(
    conn: psycopg.Connection, item_id: int, deps: Optional[Deps] = None, max_steps: int = 10
) -> List[AdvanceResult]:
    """Drive an item through all automatic transitions until it needs a human
    or reaches a terminal state. Used by ingestion to surface candidates."""
    results: List[AdvanceResult] = []
    for _ in range(max_steps):
        res = advance_by_id(conn, item_id, deps=deps)
        results.append(res)
        if res.status in ("needs_human", "terminal", "noop"):
            break
    return results
