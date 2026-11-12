"""evals/judge_model_eval.py -- Stage 2b: the score judge on one model, over labelled cards.

Runs the PRODUCTION judge call (forced tool `submit_score`, temperature 0, the loaded profile, prompt version
from the config) on the given vacancies with ``--model`` and records, per card, ONE of:
  ok            a valid score
  insufficient  the model flagged "not enough content" (a normal outcome, not an error)
  parse_error   no valid tool_use / truncated / non-numeric score (a ValueError from the client or parser)
  api_error     the API refused the call (e.g. an unsupported parameter); NOT a parse error

    python -m evals.judge_model_eval run --model claude-sonnet-5-5 --ids 309 314 ... --max-usd 0.25 [--confirm]

Prints the estimate and stops unless --confirm; hard stop at --max-usd. The DB is read-only here; the vacancy
texts and the operator's labels never leave the database. The per-card file goes to the GITIGNORED data/evals/
(labels are private); stdout carries ids, outcomes and scores only.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from typing import Any, Dict, List

from evals.common import (
    BudgetExceeded, PRIVATE, add_budget_args, est_score_call_usd, gate, live_ctx, make_client, print_usage_summary,
)
from job_hunter import llm
from job_hunter.schema_extract import from_dict

THROTTLE_S = 0.5


def classify(exc: BaseException) -> Dict[str, Any]:
    """Outcome record for a failed judge call. PURE (no I/O)."""
    if isinstance(exc, llm.InsufficientContent):
        return {"outcome": "insufficient", "detail": ""}
    if isinstance(exc, ValueError):
        msg = str(exc)
        kind = ("no_tool_use" if "no tool_use" in msg else "truncated" if "truncated" in msg
                else "not_numeric" if "not numeric" in msg else "other_parse")
        return {"outcome": "parse_error", "detail": kind, "message": msg[:200]}
    status = getattr(exc, "status_code", None)
    return {"outcome": "api_error", "detail": f"{type(exc).__name__}" + (f" {status}" if status else ""),
            "message": str(exc)[:300]}


def load_cards(conn, ids: List[int]) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT w.id, w.extracted_json, w.raw_text, w.fetched_text, "
        "(SELECT l.label FROM eval_sample s JOIN eval_labels l ON l.sample_id = s.id "
        " WHERE s.item_id = w.id AND NOT s.is_control ORDER BY s.id LIMIT 1) AS label "
        "FROM work_items w WHERE w.id = ANY(%s) ORDER BY w.id", (ids,)).fetchall()
    return [dict(r) for r in rows if r["extracted_json"]]


def score_tool_auto(client, system, user, model, max_tokens=None, temperature=None):
    """The judge call with tool_choice AUTO (Sonnet 5.5 refuses the forced {"type": "tool"}). EVAL ONLY: the
    pipeline keeps the forced call. Same prompts, same tool; the model may answer in prose instead of calling the
    tool, which then counts as a parse_error (no_tool_use). Returns the tool input dict, like complete_tool."""
    kwargs = {"temperature": temperature} if temperature is not None else {}
    resp = client._client.messages.create(
        model=model, max_tokens=max_tokens or llm.SCORE_MAX_TOKENS,
        system=llm.build_system_param(system, False),
        messages=[{"role": "user", "content": user}],
        tools=[llm.SCORE_TOOL], tool_choice={"type": "auto"}, **kwargs)
    client._record_usage(resp, model)
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == llm.SCORE_TOOL["name"]:
            if getattr(resp, "stop_reason", None) == "max_tokens":
                raise ValueError("tool call truncated by max_tokens")
            return dict(block.input)
    raise ValueError(f"no tool_use block in response (stop_reason={getattr(resp, 'stop_reason', None)})")


def cmd_run(a) -> int:
    cfg, profile, conn = live_ctx()
    version = getattr(cfg, "score_prompt_version", "v2")
    cards = load_cards(conn, a.ids)
    if len(cards) != len(a.ids):
        print(f"note: {len(a.ids) - len(cards)} requested id(s) have no extracted data and are skipped")
    est = sum(est_score_call_usd(a.model, profile, from_dict(json.loads(c["extracted_json"])), c["raw_text"],
                                 c["fetched_text"], version=version) for c in cards)
    gate(f"judge {a.model} x{len(cards)}", est, a.max_usd, a.confirm,
         notes=f"prompt {version}, tool_choice {a.tool_choice}, temperature {'omitted' if a.omit_temperature else 0}")
    client = make_client(cfg, "judge_model", model=a.model)
    results: List[Dict[str, Any]] = []
    for c in cards:
        ex = from_dict(json.loads(c["extracted_json"]))
        rec: Dict[str, Any] = {"id": c["id"], "label": c["label"]}
        try:
            if a.tool_choice == "auto":
                system = llm.build_score_system(profile, tool=True, version=version)
                data = score_tool_auto(client, system, llm.build_score_prompt(ex, c["raw_text"], c["fetched_text"]),
                                       a.model, temperature=None if a.omit_temperature else llm.SCORE_TEMPERATURE)
                out = llm.parse_score_tool_input(data)
            else:
                out = llm.llm_score(client, ex, c["raw_text"], model=a.model, profile=profile,
                                    fetched_text=c["fetched_text"], prompt_version=version)
            rec.update(outcome="ok", score=out["score"])
        except BudgetExceeded as exc:
            print(f"STOP: {exc}")
            break
        except Exception as exc:  # noqa: BLE001 - every failure kind is a data point here
            rec.update(classify(exc))
        results.append(rec)
        time.sleep(THROTTLE_S)
    print_usage_summary(client.meter)
    counts = Counter(r["outcome"] for r in results)
    print(f"\nmodel={a.model} cards={len(results)} outcomes={dict(counts)}")
    detail = Counter((r["outcome"], r.get("detail")) for r in results if r["outcome"] in ("parse_error", "api_error"))
    for (o, d), n in detail.items():
        print(f"  {o}: {d} x{n}")
    msgs = sorted({r.get("message", "") for r in results if r["outcome"] == "api_error"})
    for m in msgs:
        print(f"  api message: {m}")
    print("\nid | label | outcome | score")
    for r in results:
        print(f"{r['id']} | {r['label']} | {r['outcome']}{'/' + r['detail'] if r.get('detail') else ''} | {r.get('score', '')}")
    if a.save:
        PRIVATE.mkdir(parents=True, exist_ok=True)
        (PRIVATE / a.save).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"per-card file: data/evals/{a.save} (gitignored)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--model", required=True)
    r.add_argument("--ids", type=int, nargs="+", required=True)
    r.add_argument("--save", default="")
    r.add_argument("--omit-temperature", action="store_true",
                   help="do not send `temperature` (Sonnet 5.5 refuses it as deprecated); the run is then NOT pinned to T=0")
    r.add_argument("--tool-choice", choices=("forced", "auto"), default="forced",
                   help="forced = the production call; auto = eval-only variant for models that refuse forced tool_choice")
    add_budget_args(r)
    r.set_defaults(fn=cmd_run)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
