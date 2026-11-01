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


class UsageMeter:
    """Records the usage of every call made through a client. Attach before use."""

    def __init__(self):
        self.calls = []   # (model, usage dict)

    def attach(self, client):
        orig = client._client.messages.create

        def create(**kw):
            resp = orig(**kw)
            u = getattr(resp, "usage", None)
            self.calls.append((kw.get("model"), {
                k: int(getattr(u, k, 0) or 0)
                for k in ("input_tokens", "output_tokens",
                          "cache_creation_input_tokens", "cache_read_input_tokens")
            } if u is not None else {}))
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
        return {
            "measured": True,
            "prices_verified": PRICES_VERIFIED,
            "total_usd": round(sum(r["usd"] for r in by.values()), 4),
            "calls": len(self.calls),
            "unpriced_calls": unpriced,
            "by_model": by,
        }


# --- live-mode helpers (DB + API); imported lazily by the live commands ------

def live_env():
    """(cfg, profile, client, ro_conn) for live modes. Read-only DB session."""
    import psycopg
    from job_hunter import config, llm
    from job_hunter.profile import load_profile

    cfg = config.load_config()
    if not cfg.anthropic_api_key or not cfg.database_url:
        raise SystemExit("live mode needs ANTHROPIC_API_KEY and DATABASE_URL in the environment")
    conn = psycopg.connect(
        cfg.database_url, options="-c default_transaction_read_only=on",
        row_factory=psycopg.rows.dict_row,
    )
    client = llm.AnthropicClient(cfg.anthropic_api_key, cfg.cheap_model)
    client.meter = UsageMeter().attach(client)   # every live run reports measured cost
    return cfg, load_profile(), client, conn


def fetch_items(conn, ids: List[int]) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, relevance_score, raw_text, extracted_json, source_link "
        "FROM work_items WHERE id = ANY(%s) ORDER BY id", (ids,),
    ).fetchall()
    return list(rows)


def host_of(url: Optional[str]) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return (m.group(1).lower().removeprefix("www.") if m else "-")
