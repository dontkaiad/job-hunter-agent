"""evals/geo_facts_eval.py -- does the facts-then-guard design reproduce the operator's geo labels?

The model extracts location FACTS only (job_hunter/geo.py); a deterministic guard decides from
the candidate eligibility constraints in the private profile. This eval runs the extraction on the
labelled vacancies and compares the guard's verdict with the operator's labels: how many geo-chip
cards it cuts, how many 'подошла бы' it would wrongly cut, and how many it leaves as 'гео?'.

  offline -- decision table on synthetic facts, metrics, split and the one-look guard (no API/DB).
  extract -- live (SPENDS): facts for the labelled vacancies -> eval_geo_facts; --max-usd required,
             estimate first, --confirm to spend. Temperature 0, forced tool.
  report  -- NO API: guard vs labels on a split.
               --split dev        the 84 development vacancies (any number of looks)
               --split held-out --final   the held-out vacancies: ONE look, recorded in eval_looks
The rule is the operator's, fixed beforehand (not fitted to data); only the extraction prompt may be
adjusted, on dev. Labels, texts and the private constraints never leave the database / the profile.
"""
from __future__ import annotations

import argparse
import collections
import json
from typing import Any, Dict, List, Optional

from evals.common import (
    add_budget_args, db_ctx, est_call_usd, est_tokens, gate, make_client, print_usage_summary, report,
)
from job_hunter import geo, golden


def labelled_items(conn) -> List[Dict[str, Any]]:
    """One row per labelled vacancy (its non-control card), with split and texts."""
    rows = conn.execute(
        "SELECT DISTINCT ON (s.item_id) s.item_id, s.held_out, s.stratum, s.band, l.label, l.chips, "
        "w.raw_text, w.fetched_text FROM eval_sample s JOIN eval_labels l ON l.sample_id = s.id "
        "JOIN work_items w ON w.id = s.item_id WHERE NOT s.is_control "
        "ORDER BY s.item_id, s.wave").fetchall()
    return [dict(r) for r in rows]


def split_of(row: Dict[str, Any]) -> str:
    return "held-out" if row["held_out"] else "dev"


def metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """rows: {label, chips, verdict, reason}. PURE."""
    chips = lambda r: [c for c in (r["chips"] or "").split(",") if c]
    geo_rows = [r for r in rows if "geo" in chips(r)]
    yes_rows = [r for r in rows if r["label"] == "yes"]
    other = [r for r in rows if "geo" not in chips(r) and r["label"] != "yes"]
    cut = lambda g: sum(1 for r in g if r["verdict"] == geo.INELIGIBLE)
    unk = lambda g: sum(1 for r in g if r["verdict"] == geo.UNKNOWN)
    ok = lambda g: sum(1 for r in g if r["verdict"] == geo.ELIGIBLE)
    return {
        "items": len(rows),
        "geo_chip": {"n": len(geo_rows), "cut": cut(geo_rows), "eligible": ok(geo_rows), "unknown_geo?": unk(geo_rows)},
        "yes": {"n": len(yes_rows), "wrongly_cut": cut(yes_rows), "eligible": ok(yes_rows), "unknown_geo?": unk(yes_rows)},
        "other_no": {"n": len(other), "cut": cut(other), "eligible": ok(other), "unknown_geo?": unk(other)},
        "geo_unknown_share_overall": round(unk(rows) / len(rows), 3) if rows else None,
        "reasons_geo_chip_cut": dict(collections.Counter(r["reason"] for r in geo_rows if r["verdict"] == geo.INELIGIBLE)),
        "reasons_yes_wrongly_cut": dict(collections.Counter(r["reason"] for r in yes_rows if r["verdict"] == geo.INELIGIBLE)),
        "reasons_geo_chip_not_cut": dict(collections.Counter(r["reason"] for r in geo_rows if r["verdict"] != geo.INELIGIBLE)),
    }


# Her own labelling error, acknowledged: home-market employers' office/hybrid roles outside the
# eligible city that she nevertheless marked 'подошла бы'. Metrics are reported both ways.
LABEL_ERROR_REASON = "home-market employer, office/hybrid in a non-eligible city"


def metrics_variants(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The metrics as labelled, and without the acknowledged labelling errors. PURE."""
    errors = [r for r in rows if r["label"] == "yes" and r["reason"] == LABEL_ERROR_REASON]
    kept = [r for r in rows if r not in errors]
    return {"as_labelled": metrics(rows), "without_acknowledged_label_errors": metrics(kept),
            "acknowledged_label_errors_excluded": len(errors)}


def one_look(conn, split: str, allow_repeat: bool = False) -> None:
    """Record a look at a held-out split; refuse a second one."""
    from job_hunter.clock import now_iso

    n = conn.execute("SELECT count(*) AS n FROM eval_looks WHERE kind = 'geo_facts' AND split = %s",
                     (split,)).fetchone()["n"]
    if n and not allow_repeat:
        raise SystemExit(f"refusing: the {split} split was already looked at {n} time(s) "
                         "(pass --allow-repeat to override, and say so in the write-up)")
    conn.execute("INSERT INTO eval_looks (kind, split, created_at) VALUES ('geo_facts', %s, %s)", (split, now_iso()))
    conn.commit()


def run_offline() -> Dict[str, Any]:
    elig = geo.Eligibility(home_markets=(geo.HomeMarket(names=("home land",), office_cities=("home city",)),))
    F = geo.GeoFacts
    cases = {
        "reloc": (F(relocation_offered=True, onsite="office"), geo.ELIGIBLE),
        "visa": (F(visa_sponsorship="offered", remote_scope="country"), geo.ELIGIBLE),
        "worldwide": (F(remote_scope="worldwide"), geo.ELIGIBLE),
        "home remote": (F(employer_country="Home Land", onsite="none"), geo.ELIGIBLE),
        "home office in the eligible city": (F(employer_country="home land", onsite="hybrid", role_city="Home City"), geo.ELIGIBLE),
        "home office elsewhere": (F(employer_country="home land", onsite="office", role_city="Other City"), geo.INELIGIBLE),
        "home office city unknown": (F(employer_country="home land", onsite="office"), geo.UNKNOWN),
        "country remote": (F(remote_scope="country", remote_locations=["x"]), geo.INELIGIBLE),
        "region remote is not cut (гео?)": (F(remote_scope="region", remote_locations=["EMEA"]), geo.UNKNOWN),
        "a list of countries is a region": (F(remote_scope="country", remote_locations=["A", "B"]), geo.UNKNOWN),
        "relocation limited to citizens/permit": (F(relocation_offered=True, relocation_restriction="citizens_or_permit_only", onsite="office"), geo.INELIGIBLE),
        "relocation inside the employer's country": (F(relocation_offered=True, relocation_restriction="within_employer_country_only", onsite="hybrid"), geo.INELIGIBLE),
        "office without relocation": (F(onsite="office"), geo.INELIGIBLE),
        "hybrid without relocation": (F(onsite="hybrid", relocation_offered=False), geo.INELIGIBLE),
        "nothing stated": (F(), geo.UNKNOWN),
        "no home market configured": (F(employer_country="home land", onsite="office"), geo.INELIGIBLE),
    }
    got = {k: geo.decide(f, geo.Eligibility() if k == "no home market configured" else elig)[0] for k, (f, _) in cases.items()}
    table_ok = all(got[k] == want for k, (_, want) in cases.items())
    rows = [{"label": "no", "chips": "geo", "verdict": geo.INELIGIBLE, "reason": "a"},
            {"label": "no", "chips": "geo", "verdict": geo.UNKNOWN, "reason": "b"},
            {"label": "yes", "chips": "", "verdict": geo.INELIGIBLE, "reason": "c"},
            {"label": "yes", "chips": "", "verdict": geo.ELIGIBLE, "reason": "d"},
            {"label": "no", "chips": "fit", "verdict": geo.ELIGIBLE, "reason": "e"}]
    m = metrics(rows)
    ids = list(range(1, 2001))
    share = sum(golden.held_out(i) for i in ids) / len(ids)
    return {
        "decision_table_ok": table_ok,
        "metrics_ok": (m["geo_chip"] == {"n": 2, "cut": 1, "eligible": 0, "unknown_geo?": 1}
                       and m["yes"]["wrongly_cut"] == 1 and m["other_no"]["n"] == 1),
        "held_out_share_about_30pct": 0.25 < share < 0.35,
        "bad_enum_degrades_to_not_stated": geo.parse_facts({"remote_scope": "everywhere", "onsite": "x",
                                                            "visa_sponsorship": "maybe"}).remote_scope == "unspecified",
    }


def _needs_restriction_refresh(facts_json: str) -> bool:
    """Facts stored before `relocation_restriction` existed, where relocation or a visa was offered."""
    try:
        f = json.loads(facts_json)
    except ValueError:
        return False
    return "relocation_restriction" not in f and "offered" in (f.get("relocation_offered"), f.get("visa_sponsorship"))


def run_extract(max_usd: float, confirm: bool, refresh_relocation: bool = False) -> Dict[str, Any]:
    from evals.common import live_ctx
    from job_hunter import store
    from job_hunter.clock import now_iso

    cfg, _profile, ro = live_ctx()
    items = labelled_items(ro)
    have = {r["item_id"] for r in ro.execute("SELECT item_id FROM eval_geo_facts").fetchall()}
    todo = [r for r in items if r["item_id"] not in have]
    if refresh_relocation:
        stale = {r["item_id"] for r in ro.execute("SELECT item_id, facts_json FROM eval_geo_facts").fetchall()
                 if _needs_restriction_refresh(r["facts_json"])}
        todo = [r for r in items if r["item_id"] in stale]
    tool_tokens = est_tokens(json.dumps(geo.GEO_TOOL))
    est = sum(est_call_usd(cfg.cheap_model, geo.GEO_SYSTEM, geo.build_geo_prompt(r["raw_text"], r["fetched_text"]),
                           150, extra_tokens=tool_tokens) for r in todo)
    gate("geo facts", est, max_usd, confirm, f"{len(todo)} of {len(items)} labelled vacancies need facts (facts only, temperature 0).")
    client = make_client(cfg, "geo_facts")
    rw = store.connect(cfg.database_url)
    store.init_db(rw)
    done, outcome = 0, collections.Counter()
    from evals.common import BudgetExceeded
    for r in todo:
        client.meter.thread_cost_reset()
        try:
            facts = geo.llm_geo_facts(client, r["raw_text"], r["fetched_text"], model=cfg.cheap_model)
        except BudgetExceeded:
            outcome["stopped (budget)"] += 1
            break
        except Exception as exc:  # noqa: BLE001
            outcome[f"error {type(exc).__name__}"] += 1
            continue
        rw.execute("INSERT INTO eval_geo_facts (item_id, facts_json, model, cost_usd, created_at) VALUES (%s,%s,%s,%s,%s) "
                   "ON CONFLICT (item_id) DO " + ("UPDATE SET facts_json=EXCLUDED.facts_json, model=EXCLUDED.model, "
                   "cost_usd=EXCLUDED.cost_usd, created_at=EXCLUDED.created_at" if refresh_relocation else "NOTHING"),
                   (r["item_id"], json.dumps(facts.to_dict(), ensure_ascii=False), cfg.cheap_model,
                    client.meter.thread_cost(), now_iso()))
        rw.commit()
        done += 1
    print_usage_summary(client.meter)
    return {"to_extract": len(todo), "done": done, "outcomes": dict(outcome), "cost": client.meter.report()}


def run_report(split: str, final: bool, allow_repeat: bool) -> Dict[str, Any]:
    from job_hunter.profile import load_profile
    import psycopg

    cfg, ro = db_ctx()
    elig = load_profile().eligibility or geo.Eligibility()
    if split == "held-out":
        if not final:
            raise SystemExit("held-out is looked at ONCE, at the end: pass --final")
        rw = psycopg.connect(cfg.database_url, row_factory=psycopg.rows.dict_row)
        one_look(rw, "held-out", allow_repeat)
    items = [r for r in labelled_items(ro) if split_of(r) == split]
    facts = {r["item_id"]: json.loads(r["facts_json"]) for r in
             ro.execute("SELECT item_id, facts_json FROM eval_geo_facts").fetchall()}
    rows, missing = [], 0
    for r in items:
        if r["item_id"] not in facts:
            missing += 1
            continue
        verdict, reason = geo.decide(geo.parse_facts(facts[r["item_id"]]), elig)
        rows.append({"label": r["label"], "chips": r["chips"], "verdict": verdict, "reason": reason})
    out = {"split": split, "vacancies_in_split": len(items), "without_facts": missing,
           "home_markets_configured": len(elig.home_markets)}
    out.update(metrics_variants(rows))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("offline")
    e = sub.add_parser("extract")
    add_budget_args(e)
    e.add_argument("--refresh-relocation", action="store_true",
                   help="re-extract only stored facts that offer relocation/visa and predate relocation_restriction")
    r = sub.add_parser("report")
    r.add_argument("--split", choices=("dev", "held-out"), required=True)
    r.add_argument("--final", action="store_true")
    r.add_argument("--allow-repeat", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "offline":
        report("geo facts (offline)", run_offline())
    elif a.cmd == "extract":
        report("geo facts: extract", run_extract(a.max_usd, a.confirm, a.refresh_relocation))
    else:
        report(f"geo facts: guard vs labels ({a.split})", run_report(a.split, a.final, a.allow_repeat))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
