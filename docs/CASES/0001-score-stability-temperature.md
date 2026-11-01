# Case 0001 — Score stability: API-default temperature vs temperature 0

- **Status:** Accepted (shipped in `d7c7977`, 2026-11-01)
- **Date:** 2026-11-01
- **Code conforms to:** `job_hunter/llm.py` (`SCORE_TEMPERATURE`, `llm_score`,
  `AnthropicClient.complete` / `complete_tool`), `evals/score_stability_eval.py`
- **Baseline:** [`evals/results/score_stability_2026-11-01.json`](../../evals/results/score_stability_2026-11-01.json)

## Summary

Scoring never set a temperature, so it ran at the API default (1.0). Repeating the
same call on the same vacancy gave different scores: up to **13 points** apart on
Haiku and **3** on Sonnet. At `temperature=0` every repeat returned the same score.
Both scoring calls (the Haiku first pass and the Sonnet corridor judge) now pin
`temperature=0`. Extract, research and draft are deliberately unchanged.

## Why it matters

Scores drive a hard decision: ≥ 60 surfaces a vacancy, < 25 deletes it, and 50–70
triggers a second, more expensive judge call. A score that moves ±6 on identical
input can flip any of those, and it makes before/after comparisons of prompt or
model changes unreadable (a change looks like an improvement or a regression by
chance). During the 2026-11-01 work, differences between stored and re-run scores
(38→28, 78→72) were first put down to "non-determinism". Part of that gap is
sampling noise; this eval measures that part (the rest comes from prompt and routing
changes since the stored value was written).

## Methodology

- **What was measured:** repeatability of `llm.llm_score` (production prompt, forced
  `submit_score` tool) — the spread (max − min) of the integer score over repeated
  calls on one unchanged vacancy.
- **Design:** 3 vacancies × 5 runs × 2 models × 2 conditions = **60 API calls**.
  Conditions: *default* (no `temperature` sent → API default 1.0) and *t0*
  (`temperature=0`). Models: `claude-haiku-4-5` (first pass) and
  `claude-sonnet-4-6` (corridor judge).
- **Sample:** the three vacancies were picked from the last ~60 hours of scored
  items to span the range — one low (stored 28), one near the threshold (stored 58),
  one high (stored 78). Each has > 600 chars of text. Run on production data,
  read-only DB session, nothing written.
- **Output:** per cell the five scores plus min / max / range. Only aggregates are
  kept; no replies or rationales.

## Results

Scores over 5 runs (range in brackets):

| Vacancy (stored) | Model | Default temp (1.0) | `temperature=0` |
|---|---|---|---|
| low (28) | Haiku | 18, 28, 28, 15, 28 — **Δ13** | 18 ×5 — **Δ0** |
| ~threshold (58) | Haiku | 68, 72, 72, 68, 72 — **Δ4** | 72 ×5 — **Δ0** |
| high (78) | Haiku | 62 ×5 — Δ0 | 62 ×5 — Δ0 |
| low (28) | Sonnet | 11, 8, 8, 8, 8 — **Δ3** | 8 ×5 — **Δ0** |
| ~threshold (58) | Sonnet | 62 ×5 — Δ0 | 62 ×5 — Δ0 |
| high (78) | Sonnet | 52 ×5 — Δ0 | 52 ×5 — Δ0 |

- Default temperature: non-zero spread in **3 of 6** cells (13, 4, 3 points).
- `temperature=0`: spread **0 in 6 of 6** cells.
- The 13-point Haiku spread crosses a decision boundary: 15 and 28 are on opposite
  sides of the delete threshold 25 (in that cell, 2 of 5 runs scored below 25).

Note what the table also shows: the *stable* score is often far from the *stored*
one (e.g. Sonnet 8 vs stored 28; Haiku 72 vs stored 58). Stored values came from
earlier prompt versions and from the Haiku → corridor-judge routing, so they are
not a like-for-like reference. Temperature 0 removes noise; it does not make a score
correct.

## Limitations

- **Small n.** 3 vacancies, 5 runs each. It shows the effect exists and its rough
  size; it is not a distribution of spreads over the vacancy population. A cell with
  Δ0 at n = 5 is not proof of Δ0 at n = 500.
- **Temperature 0 is not a determinism guarantee.** It was identical across 5 runs
  on one day. A model update, or backend non-determinism at scale, can still move a
  score; re-run the eval after a model change.
- **Relevance was not measured.** There is no golden set. This eval says nothing
  about whether 72 or 62 is the *right* score. A consistently wrong score is
  consistently wrong.
- Vacancies were chosen to span the range, not sampled at random.
- Side effect to expect: with `temperature=0` the scores of vacancies that used to
  fluctuate collapse to one value, which may differ from the value recorded earlier.
  Thresholds and rubric were not touched.

**Next step:** an accuracy eval of scoring against the operator's real decisions
(vacancies they approved / sent vs skipped, as recorded in the DB states), to
measure relevance rather than repeatability.

## Reproduce

Offline harness check, no API (also runs in CI):

```bash
python -m evals offline
```

Live baseline (read-only DB, paid API, **≈ $0.3**: 30 Haiku + 30 Sonnet score calls
at the [COST.md](../../COST.md) per-call rates — an estimate, not metered):

```bash
python -m evals.score_stability_eval run --ids 10671 11097 11197 --runs 5 --save my_run.json
```
