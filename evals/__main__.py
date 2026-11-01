"""python -m evals offline -- all offline evals in one command (no API, no network, no DB).

Live runs (paid API / production DB) are per-module and explicit, never in CI:
  python -m evals.score_stability_eval run --ids <id> <id> <id>
  python -m evals.parse_robustness_eval run --ids <id> ...
  python -m evals.link_fetch_eval run
"""
from __future__ import annotations

import json
import sys

from evals import link_fetch_eval, parse_robustness_eval, score_stability_eval


def run_all():
    return {
        "score_stability": score_stability_eval.run_offline(),
        "parse_robustness": parse_robustness_eval.run_offline(),
        "link_fetch": link_fetch_eval.run_offline(),
    }


def all_ok(res) -> bool:
    return all(v for sec in res.values() for k, v in sec.items() if isinstance(v, bool))


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "offline":
        print(__doc__)
        return 2
    res = run_all()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if all_ok(res) else 1


if __name__ == "__main__":
    raise SystemExit(main())
