# Case 0005 — Heuristic vs model extract on the shadow sample (n = 21)

- **Status:** Accepted; the fix shipped in `8b21f04`, `1bd80a3`, `78a69fa` (2026-11-08)
- **Date:** 2026-11-08
- **Code conforms to:** `job_hunter/pipeline.py` (`_do_extract`, `_defer_extract`,
  `_do_reject_or_surface`), `job_hunter/store.py` (`record_extract_failure`,
  `PROTECTED_EXTRACT_MODES`, `purge_expired_heuristic`),
  `scripts/oneoff/reextract_fallback.py`
- **Baseline:** [`evals/results/extract_shadow_check_2026-11-08.json`](../../evals/results/extract_shadow_check_2026-11-08.json)

> **As of 2026-11-08.** Thresholds quoted below (< 25 delete / 25–59 keep / ≥ 60 surface) and the corridor 50–70 are those of 2026-11-08; since 2026-11-09 the surface threshold is 50 and the corridor 40–60 (Case 0006).**

## Summary

From 2026-11-02 to 11-05 the account had no API credits. Scoring was already safe in
that situation (a failure defers the vacancy, since the October postmortem, Case 0003),
but **extraction was not**: on any error `_do_extract` silently fell back to the
heuristic extractor and the pipeline carried on, scoring and — below the cut —
*deleting* vacancies on fields the model had never seen. That is the same antipattern as
`score = 0` in October: an infrastructure failure silently lowers the quality of the
input a destructive decision relies on.

We measured what it cost, on the only deleted vacancies whose text still exists (the
shadow sample): **of 21 below-the-cut vacancies re-extracted by the model and re-scored
on the current system, 19 stayed under 25, 2 would have been kept (both 28), 0 would
have surfaced; the mean moved from 12.9 to 13.3.** In this small sample the heuristic
extraction did not visibly depress scores. The fix changes the behaviour regardless:
a failing extract now defers, and a vacancy extracted without the model is never deleted
by a low score.

## What happened (numbers)

- **125 vacancies** of that cohort survived with a failed-over extraction (8 / 55 / 34 / 28
  on 2–5 Nov): 75 still unscored, 38 `rejected`, 12 `surfaced`. 135 carry the marker
  overall (10 older ones).
- **≈ 210 were deleted** from the same cohort after scoring resumed on 6 Nov. This is an
  **estimate**: 21 of the 37 shadow rows lie in the cohort's id range and the shadow
  sample is a deterministic 10 % of model-scored deletions (± ~35 %). The deleted rows
  are gone with their audit trail; **how many of them had a fallback extraction is not
  known** (the cohort also contains ≈ 126 survivors extracted by the deliberate topic
  gate, so roughly half and half, but deletion rates are not equal).
- Cause chain: no credits → `llm_extract` raises → heuristic fallback → ingested items
  wait (scoring defers) → credits return on 6 Nov → backlog scored on poor fields → cut at
  25 → deleted.

## Methodology

- **What was measured:** whether re-extracting with the model changes the verdict
  (< 25 delete / 25–59 keep / ≥ 60 surface) for vacancies the heuristic path had
  scored under the cut.
- **Sample:** every `eval_shadow` row of kind `score` whose original id lies in the
  cohort's id range (11505–12191): **n = 21**, all with a stored text and an original
  score under 25. This is a read-only check: nothing was written to `work_items`.
- **How:** model extract (`llm_extract` + the deterministic `reconcile` pass, as in
  production), then the production scoring route (Haiku first pass, temperature 0,
  forced tool; the Sonnet judge only in the 50–70 corridor — it never triggered). 42
  Haiku calls, run through the spending guard (`--max-usd 0.26`, estimate printed first,
  `--confirm`).
- **Cost (measured from API usage):** 135,605 input / 0 cache-write / 0 cache-read /
  10,524 output tokens = **$0.188** (the estimate printed before the run was $0.25).

## Results

| After the model extract | Count | Share (95 % Wilson) |
|---|---|---|
| still < 25 — deletion confirmed | 19 | 90 % (71–97 %) |
| 25–59 — would have been kept as `rejected` | 2 (both 28) | 10 % (3–29 %) |
| ≥ 60 — would have surfaced | 0 | 0 % (upper bound 15 %) |
| mean score | 12.9 → 13.3 | |

New scores, sorted: 8 ×11, 12, 15, 18 ×6, 28 ×2.

## Limitations

- **n = 21 and one run.** "No visible effect" is not "no effect": two vacancies crossed
  the 25 line, and with zero of 21 reaching 60 the true share could still be up to ~15 %.
- **Selected on the outcome.** These are vacancies the heuristic path already scored
  *under 25*. The sample can only show scores rising or staying; it cannot show the
  opposite error (heuristic extraction *inflating* a score), and it says nothing about
  the 38 `rejected` (25–59) and 12 `surfaced` survivors, where the question is a different
  threshold. That is why a re-extract of the `rejected` ones was **not** run (estimated
  $0.43): the one measurement taken gave no reason to spend it.
- **Not the whole cohort.** Only the 10 % hash-sample of deletions exists as text; the
  other ≈ 190 deleted vacancies cannot be re-examined.
- **Same scorer, not a golden set.** Old and new scores come from the same model and
  prompt (no scoring prompt change between 6 and 8 Nov; not verified byte for byte).
  This compares two inputs to one scorer. It does not say which score is *right*:
  relevance against the operator's judgement was not measured.
- Shadow rows expire after 90 days, so the run is reproducible only while they exist.

## What changed

| Before | After |
|---|---|
| any extract error → silent heuristic fallback | credits/auth: vacancy stays `discovered`, no attempt burned, extract and scoring stop for the run; rate limit / 5xx / timeout / parse: up to 3 tries, then `extract_failed` (kept, never scored, never deleted) |
| no record of how a vacancy was extracted | `extract_mode`: `llm`, `heuristic:prefilter`, `:duplicate`, `:topic`, `:no-llm` |
| a low score deleted heuristically-extracted vacancies | topic-gated vacancies cut under 25 are kept as `rejected` for 30 days, then expired; each is copied (100 %, own row cap) to the shadow set to measure the gate's recall |
| invisible | ops lines `extract: deferred N (credits / transient / parse), failed(3x) W` (red on credits/auth or > 20 % failing) and `heuristic extract: kept N, expired M` |

The topic gate stays enforced. Whether it cuts real AI roles is now measurable from the
new shadow kind; that is the next data to read.

## Cost

Measured from API usage (`llm_usage`, step `eval:reextract_shadow_check`): the shadow check, 21 extractions + 21 scorings on Haiku, **$0.188** (the pre-run estimate was $0.25). The repair itself (`reextract-unscored`, 75 Haiku extractions, `eval:reextract_unscored`) cost **$0.217**. Nothing else was spent.

## Reproduce

```bash
# in the job-hunter container, while the 2026-11 shadow rows exist (90-day TTL)
PYTHONPATH=/app python scripts/oneoff/reextract_fallback.py shadow-check --max-usd 0.26
#   prints the estimate (≈ $0.25) and stops; add --confirm to spend, hard stop at the limit
```

Offline guards for the behaviour (database only, no API): `tests/test_extract_defer.py`,
`tests/test_heuristic_keep.py`, `tests/test_reextract_scripts.py`.
