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
    return cfg, load_profile(), llm.AnthropicClient(cfg.anthropic_api_key, cfg.cheap_model), conn


def fetch_items(conn, ids: List[int]) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, relevance_score, raw_text, extracted_json, source_link "
        "FROM work_items WHERE id = ANY(%s) ORDER BY id", (ids,),
    ).fetchall()
    return list(rows)


def host_of(url: Optional[str]) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return (m.group(1).lower().removeprefix("www.") if m else "-")
