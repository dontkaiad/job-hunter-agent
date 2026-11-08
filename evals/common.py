"""Shared helpers for the evals: paths, aggregates, DB access for the live modes.

Privacy rules (the repo is public):
  * vacancy text is read from the DB at run time and NEVER written anywhere;
  * live runs print / save AGGREGATES only (counts, score spreads, host names) --
    no raw model replies, no rationales, no profile content, no Telegram ids;
  * the only files committed under evals/ are synthetic fixtures and aggregate
    baselines in evals/results/.
Live modes call the paid API and the production DB; they run ONLY on explicit
command and are never part of CI (`python -m evals offline` makes no network or
API calls).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
SYNTHETIC = ROOT / "evals" / "synthetic"
RESULTS = ROOT / "evals" / "results"
PRIVATE = ROOT / "data" / "evals"          # gitignored scratch space


def pct(a: float, b: float) -> float:
    return round(100.0 * a / b, 1) if b else 0.0


def spread(values: List[float]) -> Dict[str, Any]:
    """min / max / range of a list of numeric runs (None if empty)."""
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return {"min": None, "max": None, "range": None, "runs": len(values)}
    return {"min": min(nums), "max": max(nums), "range": max(nums) - min(nums), "runs": len(values)}


def report(title: str, data: Dict[str, Any]) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def save_result(name: str, data: Dict[str, Any]) -> Path:
    """Write an AGGREGATE result under evals/results/ (committed baselines)."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / name
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --- measured API cost -------------------------------------------------------
# Live runs report the cost they ACTUALLY incurred, summed from the `usage` block of
# every API response -- never a modelled figure. Prices are USD per million tokens,
# verified against the official pricing page on PRICES_VERIFIED; re-check them when
# the models change (an unknown model is reported as unpriced, not as $0).
PRICES_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
PRICES_VERIFIED = "2026-11-01"
PRICES = {
    "claude-haiku-4-5": {"input": 1.00, "cache_write_5m": 1.25, "cache_read": 0.10, "output": 5.00},
    "claude-sonnet-4-6": {"input": 3.00, "cache_write_5m": 3.75, "cache_read": 0.30, "output": 15.00},
}


def price_for(model):
    """Price row for ``model`` (exact id, or a dated variant of it); None if unknown."""
    if model in PRICES:
        return PRICES[model]
    for name, row in PRICES.items():
        if (model or "").startswith(name):
            return row
    return None


def usage_cost(model, usage):
    """USD for one response's usage dict (None if the model is unpriced). PURE."""
    p = price_for(model)
    if p is None:
        return None
    return (
        usage.get("input_tokens", 0) * p["input"]
        + usage.get("cache_creation_input_tokens", 0) * p["cache_write_5m"]
        + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
        + usage.get("output_tokens", 0) * p["output"]
    ) / 1_000_000


class BudgetExceeded(RuntimeError):
    """Raised BEFORE an API call once the run has spent its --max-usd."""


class UsageMeter:
    """Records the usage of every call made through a client, enforces a spending
    limit, and is thread-safe. Attach before use."""

    def __init__(self, max_usd=None):
        import threading

        self.calls = []   # (model, usage dict)
        self.max_usd = max_usd
        self._spent = 0.0
        self._lock = threading.Lock()
        self._tl = threading.local()      # per-thread running cost (per-item accounting)

    def thread_cost_reset(self):
        self._tl.cost = 0.0

    def thread_cost(self):
        return getattr(self._tl, "cost", 0.0)

    def spent_usd(self):
        return self._spent

    def attach(self, client):
        orig = client._client.messages.create

        def create(**kw):
            if self.max_usd is not None and self._spent >= self.max_usd:
                raise BudgetExceeded(
                    f"budget reached: spent ${self._spent:.4f} of --max-usd ${self.max_usd:.2f}; stopped")
            resp = orig(**kw)
            u = getattr(resp, "usage", None)
            usage = {
                k: int(getattr(u, k, 0) or 0)
                for k in ("input_tokens", "output_tokens",
                          "cache_creation_input_tokens", "cache_read_input_tokens")
            } if u is not None else {}
            cost = usage_cost(kw.get("model"), usage) or 0.0
            self._tl.cost = getattr(self._tl, "cost", 0.0) + cost
            with self._lock:
                self.calls.append((kw.get("model"), usage))
                self._spent += cost
            return resp

        client._client.messages.create = create
        return self

    def report(self):
        """Measured totals per model + overall (aggregates only)."""
        by = {}
        unpriced = 0
        for model, usage in self.calls:
            row = by.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                        "cache_creation_input_tokens": 0,
                                        "cache_read_input_tokens": 0, "usd": 0.0})
            row["calls"] += 1
            for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                      "cache_read_input_tokens"):
                row[k] += usage.get(k, 0)
            c = usage_cost(model, usage)
            if c is None:
                unpriced += 1
            else:
                row["usd"] += c
        for row in by.values():
            row["usd"] = round(row["usd"], 4)
        tot = {k: sum(r[k] for r in by.values()) for k in
               ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}
        return {
            "measured": True,
            "limit_usd": self.max_usd,
            "tokens_total": {"input": tot["input_tokens"], "cache_write": tot["cache_creation_input_tokens"],
                             "cache_read": tot["cache_read_input_tokens"], "output": tot["output_tokens"]},
            "prices_verified": PRICES_VERIFIED,
            "total_usd": round(sum(r["usd"] for r in by.values()), 4),
            "calls": len(self.calls),
            "unpriced_calls": unpriced,
            "by_model": by,
        }


# --- live-mode helpers (DB + API); imported lazily by the live commands ------

# --- spending guard ----------------------------------------------------------
# Any script that calls the Anthropic API outside the scheduled harvest goes through
# gate() (a --max-usd is mandatory, a cost estimate is printed from the real input
# sizes, and nothing is spent without --confirm) and make_client() (a hard stop at the
# limit; usage is logged to llm_usage under an 'eval:<name>' step).
TOKEN_CALIBRATION = 1.085   # measured/predicted on 366 real Haiku score calls (offline estimator undershoots 8.5%)
_GATE = {}


def est_tokens(text):
    from job_hunter import llm
    return llm.estimate_tokens(text or "")


def est_call_usd(model, system, user, out_tokens, *, extra_tokens=0, batch=False):
    """Conservative USD for one call: no prompt-cache credit, offline token estimate
    x TOKEN_CALIBRATION, a fixed expected output length. PURE."""
    p = price_for(model)
    if p is None:
        raise ValueError(f"no price for model {model!r}")
    inp = (est_tokens(system) + est_tokens(user) + extra_tokens) * TOKEN_CALIBRATION
    usd = (inp * p["input"] + out_tokens * p["output"]) / 1_000_000
    return usd * (0.5 if batch else 1.0)


def est_score_call_usd(model, profile, extracted, raw, fetched=None, *, batch=False):
    """USD to score one vacancy with the production tool prompt on ``model``."""
    from job_hunter import llm

    system = llm.build_score_system(profile, tool=True)
    user = llm.build_score_prompt(extracted, raw or "", fetched)
    out = 420 if "haiku" in model else 435          # measured means (cost_probe)
    return est_call_usd(model, system, user, out,
                        extra_tokens=est_tokens(json.dumps(llm.SCORE_TOOL)), batch=batch)


def gate(label, estimate_usd, max_usd, confirm, notes=""):
    """The mandatory pre-flight of every API-calling script. Exits unless the run is
    explicitly confirmed AND within its limit; prints the estimate either way."""
    if max_usd is None or max_usd <= 0:
        raise SystemExit("refusing to start: --max-usd N is required for any script that calls the API")
    print(f"[{label}] ESTIMATE ${estimate_usd:.2f} (upper-bound style: no cache credit, calibration "
          f"x{TOKEN_CALIBRATION}); limit --max-usd ${max_usd:.2f}. {notes}".strip(), flush=True)
    if estimate_usd > max_usd:
        raise SystemExit(f"refusing to start: the estimate ${estimate_usd:.2f} exceeds --max-usd "
                         f"${max_usd:.2f}. Raise the limit or shrink the run.")
    if not confirm:
        print(f"[{label}] estimate only - NO API call was made. To spend (stopping at "
              f"${max_usd:.2f}) re-run with --confirm.", flush=True)
        raise SystemExit(0)
    _GATE.clear()
    _GATE.update(label=label, max_usd=max_usd)


def make_client(cfg, step, model=None):
    """The ONLY way evals get an API client. Needs a passed gate(); stops at the limit;
    logs every call's tokens to llm_usage as 'eval:<step>:<pipeline step>'."""
    from job_hunter import llm, store

    if not _GATE:
        raise RuntimeError("API client requested without a passed gate() (no --max-usd/--confirm)")
    base = store.make_usage_sink(cfg.database_url)

    def sink(rec):
        base({**rec, "step": f"eval:{step}:{rec.get('step', 'other')}"})

    client = llm.AnthropicClient(cfg.anthropic_api_key, model or cfg.cheap_model, usage_sink=sink)
    client.meter = UsageMeter(max_usd=_GATE["max_usd"]).attach(client)
    return client


def print_usage_summary(meter):
    """End-of-run line: tokens (input / cache write / cache read / output) and $."""
    r = meter.report()
    t = r["tokens_total"]
    print(f"[usage] calls={r['calls']} input={t['input']} cache_write={t['cache_write']} "
          f"cache_read={t['cache_read']} output={t['output']} -> ${r['total_usd']:.4f} "
          f"(limit ${r['limit_usd']})", flush=True)


def live_ctx():
    """(cfg, profile, ro_conn) for live modes: read-only DB, NO API client."""
    import psycopg
    from job_hunter import config
    from job_hunter.profile import load_profile

    cfg = config.load_config()
    if not cfg.anthropic_api_key or not cfg.database_url:
        raise SystemExit("live mode needs ANTHROPIC_API_KEY and DATABASE_URL in the environment")
    conn = psycopg.connect(
        cfg.database_url, options="-c default_transaction_read_only=on",
        row_factory=psycopg.rows.dict_row,
    )
    return cfg, load_profile(), conn


def db_ctx():
    """(cfg, read-only conn) for steps that never touch the API (no key needed)."""
    import psycopg
    from job_hunter import config

    cfg = config.load_config()
    if not cfg.database_url:
        raise SystemExit("needs DATABASE_URL in the environment")
    return cfg, psycopg.connect(cfg.database_url, options="-c default_transaction_read_only=on",
                                row_factory=psycopg.rows.dict_row)


def add_budget_args(parser):
    parser.add_argument("--max-usd", type=float, required=True,
                        help="hard spending limit for this run (required)")
    parser.add_argument("--confirm", action="store_true",
                        help="actually spend; without it only the estimate is printed")


def fetch_items(conn, ids: List[int]) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, relevance_score, raw_text, extracted_json, source_link, source_channel "
        "FROM work_items WHERE id = ANY(%s) ORDER BY id", (ids,),
    ).fetchall()
    return list(rows)


def host_of(url: Optional[str]) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return (m.group(1).lower().removeprefix("www.") if m else "-")
