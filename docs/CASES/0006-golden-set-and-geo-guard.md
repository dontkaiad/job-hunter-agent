# Case 0006 — Golden set: how good is the score, and the geo guard

- **Status:** Accepted; follow-ups shipped on 2026-11-09 (surface threshold 60 → 50, geo guard in the harvest)
- **Date:** 2026-11-09
- **Code conforms to:** `job_hunter/golden.py` (`design_estimate`, `design_interval`, `cohen_kappa`),
  `evals/golden_eval.py` (`report`), `job_hunter/geo.py`, `evals/geo_facts_eval.py`
- **Baselines:** [`golden_report_2026-11-09.json`](../../evals/results/golden_report_2026-11-09.json),
  [`geo_guard_2026-11-09.json`](../../evals/results/geo_guard_2026-11-09.json)

## Summary

Cases 0001–0005 measured repeatability and parseability, not whether a score is *right*.
This case compares the current scorer with one operator's labels on 115 vacancies.

- **Precision@60 ≈ 0.56** (95 %: 0.21–0.82) and **recall@60 ≈ 0.17** (0.09–0.48). The
  intervals are wide; the point estimates are not a claim of accuracy.
- **Score 50–59 is as relevant as 70–79**: 12 of 18 decided cards (67 %, 0.44–0.84) against
  5 of 10 (50 %). Moving the threshold 60 → 50 gives precision 0.62 (0.27–0.81) and recall
  0.24 (0.14–0.57). It was moved (separate, revertible commit; the judge corridor follows: [40, 60]).
- Of 20 rejected cards at score ≥ 60, **15 were rejected only for geography, staleness or both**,
  things the scorer cannot see from text: hence the deterministic geo guard.
- The operator's label noise is high (κ 0.34 over three classes, 0.44 binary, 15 control pairs).

## Methodology

**Population.** The sampling frame is every scored, deduplicated vacancy with enough text
(≈ 1 700 rows), excluding vacancies restored after the October outage. Each is assigned to a score
band; the sample is stratified by band (`<25`, `25–39`, `40–49`, `50–59`, `60–69`, `70–79`, `80+`),
with more cards where the decision boundary is (50–69). 120 vacancies in two waves, plus 15
control cards that repeat earlier ones for the label-noise check. The operator labels blind
(`yes / no / unsure`, plus reason chips on `no`) in the dashboard; the score is not shown.

**Re-scoring.** The bands were first assigned from stored scores of mixed system versions. All
labelled vacancies were then re-scored on the *current* system (Haiku first pass, Sonnet judge in
the corridor, temperature 0): that is the score every number below uses.

**Classes.** `yes` → relevant. `no` with a *fit* or *seniority* chip (or no chip) → irrelevant.
`no` carrying only *geo / stale / pay* → **excluded** (not a verdict on role fit: freshness is
unknowable from text, pay and geography are deterministic guards). `other` and `unsure`
separate. In the main metric only relevant/irrelevant are *decided*; a second line counts
excluded/other as irrelevant (the strict reading).

**Estimator.** Each sampling band stands for its frame size N_h; within a band the decided
share of cards is scaled up. Precision@T and recall@T are computed from the weighted
(band × above/below T) cells. Intervals: 1 500 draws from a Dirichlet(count + ½) per band
(a band with no relevant card keeps honest uncertainty), 2.5–97.5 %. Label noise: a symmetric
flip rate ε corrects each observed share as (p − ε)/(1 − 2ε).

**n.** 115 labelled core vacancies: 31 relevant, 42 irrelevant, 39 excluded, 2 other, 1 unsure.
Frame sizes: `<25` 164, `25–39` 1 170, `40–49` 139, `50–59` 36, `60–69` 3, `70–79` 145, `80+` 20.

## Results

| Threshold | Precision (95 %) | Recall (95 %) |
|---|---|---|
| 50 | 0.62 (0.27–0.81) | 0.24 (0.14–0.57) |
| 55 | 0.56 (0.21–0.82) | 0.17 (0.09–0.49) |
| 60 | 0.57 (0.21–0.82) | 0.17 (0.09–0.48) |
| 70 | 0.56 (0.21–0.82) | 0.17 (0.09–0.48) |

Strict reading (geo/stale/pay/other counted as irrelevant): precision@60 **0.26** (0.14–0.60),
@50 0.30, recall unchanged.

Calibration (relevant share among decided cards, by current score; Wilson 95 %):

| Band | decided | relevant | share |
|---|---|---|---|
| < 25 | 9 | 2 | 22 % |
| 25–39 | 23 | 6 | 26 % (13–47 %) |
| 40–49 | 9 | 2 | 22 % |
| 50–59 | 18 | 12 | **67 %** (44–84 %) |
| 60–69 | 1 | 1 | – |
| 70–79 | 10 | 5 | 50 % (24–76 %) |
| 80+ | 3 | 3 | – |

Recall@60 is driven by the `25–39` band, which holds 1 170 of ≈ 1 700 vacancies: 6 relevant of 23
decided there already means a few hundred relevant vacancies below the threshold.

**Label noise.** 15 control pairs: agreement 80 % (3 classes), κ = 0.34; without `unsure`
(14 pairs) 86 %, κ = 0.44. A 5–15 % flip rate leaves precision at ≈ 0.56 and lifts recall from
0.17 to 0.36. One labeller, no adjudication.

### Geo guard (facts from the model, verdict from a deterministic function)

The model only extracts facts (relocation / visa offered and restricted?, remote scope and
places, on-site requirement, role city, employer country/city); the verdict is a pure function of
those facts and the candidate's eligibility constraints, which live in a private profile and
not in the code. `UNKNOWN` is never a reject: the card is shown, flagged "гео?".

| Split | n | Geo-chip cards cut / unknown / eligible | "yes" cards wrongly cut |
|---|---|---|---|
| dev (as labelled) | 87 | 10 / 10 / 9 of 29 | 3 of 21 |
| dev (without 3 acknowledged label errors) | 84 | 10 / 10 / 9 of 29 | **0 of 18** |
| held-out (as labelled) | 33 | 6 / 2 / 3 of 11 | 2 of 10 |
| held-out (without 1 acknowledged label error) | 32 | 6 / 2 / 3 of 11 | 1 of 9 |

"Acknowledged label errors": three dev cards (one held-out) that the operator labelled "would
fit" although the operator's own stated rule cuts them; the operator confirmed the labels are wrong and **they were
not changed**: both lines are reported. `unknown` is 32–40 % of all cards.

**The rules were fixed before the held-out look.** Three changes were made after the dev run
and before the held-out one: whole-word matching (a substring bug let `us` match `trust`);
relocation counts only when not restricted to citizens/permit holders or to moves inside the
employer's country; a regional remote (a region or several countries) is `UNKNOWN`, not a cut.
Held-out was then looked at once (`--final`, recorded in `eval_looks`). The tuning set was
dev only.

Cost of the whole phase: re-scoring the labelled cards $0.75, geo facts $0.33 + $0.01.

## Follow-up: score prompt v1 → v2 (not proven; enabled by the owner's decision; monitored)

**What changed in v2** (`job_hunter/llm.py`, `SCORE_PROMPT_VERSIONS`): location, remote scope,
relocation, visa and timezone are not scored (the geo guard decides them), and an unstated salary is
not a reason to lower the score. The location wording was also removed from the calibration anchors
(same four numeric levels). Hypotheses about thin postings and seniority were deliberately left out
to isolate the effect. Public diff: `python -m evals.score_prompt_eval diff`.

**Design.** Dev split only (86 vacancies; 75 paired core cards after 5 `insufficient` and 6 without a
stored v1 score). "Before" = the stored v1 score from the same judge corridor [50, 70]; "after" = v2
with that corridor, so only the prompt differs. Held-out was not used. Cost $0.80, 110 calls.

| Dev, threshold 50 | v1 | v2 |
|---|---|---|
| precision, unweighted (decided cards) | 0.61 | 0.55 |
| recall, unweighted | 0.74 | 0.84 |
| precision, design-weighted (95 %) | 0.60 (0.23–0.83) | 0.54 (0.23–0.77) |
| recall, design-weighted (95 %) | 0.26 (0.12–0.62) | 0.27 (0.14–0.66) |

Across the threshold: of 5 relevant cards below 50, **2 rose above it**; of 20 irrelevant cards below
50, **4 rose above it**; none fell below. Mean shift: relevant +9.0, others −0.8. Relevant share in
the 70–79 band 9/15 (v1 4/8); the 50–59 band thinned out (2/7 against 7/12).

**Verdict: not proven.** v2 moves relevant cards up more than irrelevant ones, but the weighted
precision and recall are indistinguishable (intervals overlap almost entirely) and n is small; the
two changes are not separated. **Enabled in production by the owner's decision** (a missed vacancy
costs more than an extra card), through `SCORE_PROMPT_VERSION` (default `v2`; rollback =
set `SCORE_PROMPT_VERSION=v1`, one variable). To be watched: share of surfaced cards the operator
rejects for fit/seniority, and the new decisions captured as labels (see the dashboard-label
mechanism) as the check on whether precision dropped.

### Live labels: the operator's own decisions

From 2026-11-09 every dashboard «Одобрить» / «Отклонить» (with reason chips) is also written to
`eval_decision_labels` (approve → yes, reject → no + chips) with provenance (channel, actor, state,
score shown, geo verdict, score prompt version, threshold) and the vacancy is frozen in
`eval_vacancies` (`origin = 'decision'`). No API calls. Aggregates: `python -m evals.golden_eval decisions`.
These labels cover only cards that were surfaced: they measure the **precision of what was shown**
(useful to see whether v2 / T = 50 made it worse) and cannot measure recall; the stratified,
blind golden set stays the instrument for that.

## Limitations

- **One labeller, κ ≈ 0.34–0.44.** The "ground truth" is itself noisy; recall in particular
  moves a lot with the assumed noise.
- **Small sample.** ~29 labelled cards at score ≥ 60 and 2 in the 60–69 band; every interval is
  wide and the 50 vs 60 comparison is suggestive, not significant. Intervals overlap.
- **Weights use the current-score band, sampling used the band at sampling time** (a mix of
  stored and re-scored values for part of the sample). A few vacancies changed band after the
  re-score, so the weights are approximate. The frame also drifts as the harvest adds vacancies
  (numbers differ in the third digit between two runs a day apart).
- **The "excluded" class is a modelling choice.** The main metric leaves geo/stale/pay-only
  rejections out; the strict reading puts them in and halves precision.
- **Geo guard:** 33 held-out cards, 11 with a geo chip; a single-country remote that the
  operator would take is cut by the rule as stated; the held-out "yes" cut for that reason is
  a rule/label disagreement, not an extraction error.
- **No second look at held-out.** Prompt changes made from here on are tuned on dev only.

## Reproduce

```bash
python -m evals.golden_eval report --exclude-urls <urls file>      # live DB, no API
python -m evals.geo_facts_eval report --split dev                  # live DB, no API (after facts exist)
python -m evals.geo_facts_eval report --split held-out --final     # ONCE
python -m evals offline                                            # offline checks, CI
```
