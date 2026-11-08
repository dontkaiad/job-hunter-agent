# Cases — evals and postmortems

Each case = a reproducible script in [`evals/`](../../evals/), a committed aggregate
baseline in [`evals/results/`](../../evals/results/), and a write-up here with the
same sections: **summary → methodology (what, how, n) → results → limitations →
reproduce**.

| # | Case | Type | Script |
|---|---|---|---|
| [0001](0001-score-stability-temperature.md) | Score stability: temperature default vs 0 | eval | `evals/score_stability_eval.py` |
| [0002](0002-parse-robustness-text-vs-tool.md) | Parse robustness: text JSON vs forced tool use | eval | `evals/parse_robustness_eval.py` |
| [0003](0003-postmortem-2026-10-credit-outage.md) | Postmortem: credit outage → deleted vacancies | postmortem | regression tests |
| [0004](0004-link-only-fetch.md) | Link-only stubs: fetching the linked page | eval | `evals/link_fetch_eval.py` |
| [0005](0005-heuristic-vs-model-extract.md) | Heuristic vs model extract on the shadow sample (n = 21) | eval + fix | `scripts/oneoff/reextract_fallback.py` |

For the general method (what an eval is, how to choose a metric) see
[../EVALS.md](../EVALS.md).

## What these evals do not measure

**Scoring relevance.** There is no golden set. 0001–0004 measure repeatability,
parseability and text recovery — not whether a 72 is the right score. The planned
next eval compares the score against the operator's real approve / skip decisions
recorded in the DB.

## Running

```bash
python -m evals offline          # all offline checks; no API, no network, no DB; runs in CI
```

Live runs call the paid API and the production DB (read-only) and are **never run
in CI**. Every one of them needs `--max-usd N` (it refuses to start without it),
prints a cost estimate from the real input sizes and stops, spends only with
`--confirm`, stops at the limit, and ends with the measured tokens
(input / cache write / cache read / output) and dollars; usage is also logged to
`llm_usage` as `eval:<name>`. Their commands and approximate cost are in each case. Live runs print and
save aggregates only; vacancy text and model replies are never written to the repo.
