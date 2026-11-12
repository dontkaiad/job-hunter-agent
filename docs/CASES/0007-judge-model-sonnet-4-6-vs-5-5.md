# Case 0007 — Score judge: Sonnet 4.6 vs Sonnet 5.5 (a "not worse" check)

- **Status:** Accepted. Decision: keep Sonnet 4.6; nothing changed in production.
- **Date:** 2026-11-12
- **Code conforms to:** `evals/judge_model_eval.py` (`run`, `score_tool_auto`, `classify`), `evals/common.py` (`PRICES`)
- **Baseline:** [`judge_model_2026-11-12.json`](../../evals/results/judge_model_2026-11-12.json)

## Summary

**Question.** The corridor judge runs on Claude Sonnet 4.6. A newer model, Sonnet 5.5, is cheaper per token. Is it at
least *not worse* as the judge, so that a switch could be considered?

**Answer.** Not on this evidence. On 38 labelled vacancies where the judge is actually called, 5.5 matched 4.6 on
recall (both 15 of 18 positives) but surfaced three more negatives (precision 0.52 vs 0.58, specificity 0.30 vs 0.45),
and a call cost 43 % more in the measured setup. Under the decision rule written before the run, that is **"keep 4.6"**.
This is a *not-worse* check on a small sample, not proof of equivalence or of superiority in either direction.

**Two findings that mattered more than the numbers.**
1. **Sonnet 5.5 cannot be called the way production calls the judge.** The API rejects a forced
   `tool_choice` (`type: "tool"`) and rejects `temperature` (HTTP 400, both times; the requests were refused before billing,
   $0). The production judge depends on both: a forced tool call makes prose answers impossible
   ([Case 0002](0002-parse-robustness-text-vs-tool.md)) and temperature 0 makes the score repeatable
   ([Case 0001](0001-score-stability-temperature.md)). Any switch to 5.5 needs a code change to the call, and with
   `tool_choice: auto` the model *may* answer in prose again.
2. **A suspected "35 % parse errors" did not reproduce.** The figure came from the maintainer's notes, not from the repo.
   With `tool_choice: auto` and no temperature, 5.5 gave 0 parse errors on a 12-vacancy slice (11 scores, 1
   `insufficient_content`) and 1 on the 38 (a reply cut at the 800-token output cap). Had the true rate been 35 %, seeing
   none in 12 would have had a probability of about 0.6 %.

## Methodology

- **What was measured.** The production judge call (`submit_score` tool, the loaded private profile, prompt version v2) on
  the same vacancies with each model: per-vacancy outcome (`ok` / `insufficient` / `parse_error` / `api_error`) and, where
  there is a score, the score.
- **Sample.** 38 vacancies from the labelled golden set ([Case 0006](0006-golden-set-and-geo-guard.md)): the labelled
  vacancies on which the judge was actually invoked in the earlier scoring run, i.e. the borderline cases where the judge
  decides something (earlier scores 22–62). **18 positives, 20 negatives**, one labeller. Control items and "unsure"
  labels are excluded. This is a *hard* subset, not a random one, and it is the judge alone, not the whole pipeline (the
  pipeline runs a Haiku first pass and calls the judge only inside a corridor).
- **Arms.**
  - Sonnet 4.6: the production call (forced tool, temperature 0, system prompt cached).
  - Sonnet 5.5: `tool_choice: auto`, `temperature` not sent, **no prompt caching** (the eval helper does not cache).
- **Metric.** Positive = the labeller said "yes". A vacancy is *surfaced* at a score of 50 or more (the production
  threshold). A non-`ok` outcome counts as *not surfaced* (production parks `insufficient` and defers a parse error).
  Recall, precision and specificity with 95 % Wilson intervals; paired score differences.
- **Decision rule, recorded in the maintainer's plan of 2026-11-12 before the first paid call**, not changed afterwards:
  1. **Barrier:** parse errors + API errors of 5.5 above 5 % means the candidate is rejected.
  2. **Not worse:** recall@50 and precision@50 of 5.5 must each be no more than 3 percentage points below 4.6.
  3. **Cost:** the cost of a 5.5 call must not exceed that of a 4.6 call (from the tokens measured in the run).
  4. **Outcome:** recommend a switch only if 1, 2 and 3 all hold; otherwise keep 4.6. Production is not changed by this
     case in either direction.
- **Spending guard.** `--max-usd` per arm (estimate printed first, spend only with `--confirm`, hard stop at the limit).

## Results

| | Sonnet 4.6 (production call) | Sonnet 5.5 (auto, no temperature) |
|---|---|---|
| outcomes (n = 38) | 36 ok, 2 insufficient | 37 ok, 1 parse error (output cut at 800 tokens) |
| recall @ 50 | 0.833 (15/18; 95 % CI 0.61–0.94) | 0.833 (15/18; same interval) |
| precision @ 50 | 0.577 (15/26; CI 0.39–0.74) | 0.517 (15/29; CI 0.34–0.69) |
| specificity @ 50 | 0.450 (9/20; CI 0.26–0.66) | 0.300 (6/20; CI 0.15–0.52) |
| input / output tokens per call | ≈ 4 340 (incl. 3 050 cache reads) / 442 | ≈ 5 550 / 528 |
| measured cost per call | $0.0115 | $0.0164 |
| arm total | $0.436 | $0.622 |

- **Difference 5.5 − 4.6:** recall 0.0 pp, precision −6.0 pp, specificity −15.0 pp. On the 35 vacancies both arms
  scored: recall 0.0 pp, precision −4.1 pp.
- **Where the extra false positives come from.** Three negatives were surfaced by 5.5 and not by 4.6: two scored just over
  the threshold (52 and 55 against 38 for 4.6) and one that 4.6 had flagged as `insufficient` got a 66. The paired score
  differences are small on average (mean +0.1, median 0, mean absolute 6.2 points); **4 of 35 pairs differ by 15 points or
  more** and **2 pairs fall on opposite sides of the threshold**.
- **Failures.** 5.5: 1 parse error of 38 (2.6 %), 0 API errors. The reason is length: its answers are about 20 % longer
  than 4.6's, and one hit the 800-token output cap, so a switch would also mean raising that cap.
- **Decision rule.**

| Rule | 5.5 vs 4.6 | Verdict |
|---|---|---|
| 1. parse + API errors ≤ 5 % | 2.6 % | pass |
| 2. recall and precision within 3 pp | recall 0.0 pp, precision −6.0 pp | **fail** |
| 3. cost per call not higher | $0.0164 vs $0.0115 (+43 %) | **fail** |
| 4. recommend a switch only if 1–3 hold | | **keep 4.6** |

## Limitations

- **Small sample, weak resolution.** There are only 18 positives: **one vacancy moves recall by 5.6 percentage
  points** (and one of 20 negatives moves specificity by 5.0). The 3-point margin in the rule is smaller than one
  vacancy, so on this sample any single worse case already breaks it. The precision gap is three vacancies. The
  intervals above overlap almost completely (recall is identical; the precision intervals overlap).
- **This is a *not-worse* check, not evidence of equivalence, and not a demonstration that 4.6 is better.** The sample
  cannot distinguish a real small difference from noise; it only fails to show that 5.5 is at least as good.
- **One labeller, one run per arm.** The labels are one person's; the labelled set is a hard, selected subset, not
  representative of all vacancies; nothing was repeated.
- **Sonnet 5.5's temperature is not pinned.** The API refuses `temperature`, so 5.5 ran with the model's own default
  and a second run could score differently. The two arms therefore differ in more than the model: tool choice,
  temperature and caching are all different, by necessity.
- **The cost comparison is unfair to 5.5 in one respect and unmeasured in another.** 4.6 ran with the system prompt
  cached (about 70 % of its input was cache reads); 5.5 ran uncached. The same text also costs about 28 % more
  input tokens on 5.5. Priced uncached, 4.6 would have cost about $0.020 per call, i.e. *more* than 5.5. Whether caching
  would make 5.5 cheaper than the cached 4.6 was not measured. The rule (cost from the measured run) fails as written;
  that is reported, not adjusted.
- **Earlier cost estimates were wrong in both directions.** The offline estimator, which gives no cache credit,
  under-estimated 5.5 (about ×1.3) and over-estimated cached 4.6; budgets were set from measurements, not from the
  estimate.
- **Not a pipeline result.** It is the judge alone on every vacancy; the pipeline's precision and recall also depend on
  the Haiku first pass and the corridor.

## Cost

| Step | Cost |
|---|---|
| two refused attempts on 12 vacancies (forced tool choice; temperature) | $0.00 (rejected before billing) |
| 12-vacancy slice, `tool_choice: auto`, no temperature | $0.20 |
| comparison, Sonnet 4.6, 38 calls | $0.44 |
| comparison, Sonnet 5.5, 38 calls | $0.62 |
| **total** | **≈ $1.26** |

Limits used: `--max-usd 0.25` for the slice and `0.80` per arm of the comparison. Usage is logged to `llm_usage` as
`eval:judge_model`.

## Reproduce

Offline (the failure classifier, the call shape, the price row; no API, runs in CI):

```bash
python -m pytest tests/test_judge_model_eval.py
```

Live (paid API, read-only DB; each arm needs its own `--max-usd` and prints the estimate first):

```bash
python -m evals.judge_model_eval run --model claude-sonnet-4-6 --ids <vacancy ids> --max-usd 0.80 --confirm
python -m evals.judge_model_eval run --model claude-sonnet-5-5 --max-usd 0.80 --tool-choice auto --omit-temperature \
    --ids <vacancy ids> --confirm
```

The vacancy ids come from the maintainer's private labelled set and are not published; the script prints ids, outcomes
and scores only (never vacancy text).
