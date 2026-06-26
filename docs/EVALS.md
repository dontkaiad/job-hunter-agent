# Evaluations

## What an eval is

Regular code gets tested with assertions: you know the correct output, you check
for it. LLM output is probabilistic — there is no single right answer, and the
output shifts with every model update, prompt change, or temperature tweak.

An eval measures output *quality* across a sample set, turning a subjective
judgment into a number. That number lets you compare: model A vs. model B,
prompt v1 vs. v2, before vs. after a temperature change. The comparison is the
point — the absolute number rarely matters.

Common reference points: a stronger model (used as a gold-standard judge),
a human-labeled golden set, a deterministic rule, or a previous model version
you trust.

## Why evals matter

- **Cost efficiency.** Can a cheaper model replace an expensive one without
  degrading the decision quality that the product depends on?
- **Regression testing.** You changed the prompt — did the output actually
  improve, or did something silently break elsewhere?
- **A/B routing.** Run two model variants in parallel; the eval tells you which
  one to keep.
- **Anti-hallucination.** Ground truth comparison catches a model confidently
  returning wrong structured data.

Production teams fear silent LLM degradation — a model update or API change
that slightly shifts outputs in ways no assertion catches. An eval is the
insurance policy.

## Core principle

**An eval is only as good as its metric. A bad metric gives false confidence —
which is worse than no eval, because you trust it.**

Picking the metric is the hardest part of eval design. It requires understanding
*what the model output is actually used for downstream*, not just whether the
numbers look close.

---

## Case 1: cost-aware vacancy scoring

### Context

Each job vacancy is scored 0–100 for relevance to the candidate profile. The
score drives a binary surface/reject decision at threshold 60. Originally every
vacancy went through Sonnet (expensive). The goal: replace with Haiku (cheap,
~20× cheaper) without losing decision quality where it matters.

### The first metric was wrong

Initial eval compared Haiku scores against stored Sonnet scores using **MAE
(mean absolute error)**:

```
MAE = mean(|haiku_score − sonnet_score|)
```

MAE came back 12.9. The eval printed `✅ safe`. **That was a false positive.**

MAE averages the *magnitude* of errors and is completely blind to *direction*.
Haiku was systematically over-scoring every vacancy by ~+13 points. MAE saw
"errors are around 13 points on average" — which sounds bad but hit the
threshold — and called it safe.

### Bias vs. variance

Two distinct failure modes, different fixes:

| | Description | Fix |
|---|---|---|
| **Bias** | Systematic shift in one direction (model consistently over- or under-scores) | Calibration: add anchor examples to the prompt |
| **Variance** | Spread around the truth; near the decision threshold, this flips surface/reject decisions incorrectly | Routing: send uncertain cases to a stronger model |

Bias hides inside MAE because positive and negative errors average out to a
large magnitude but the *direction* is invisible. You need signed metrics.

### Honest metrics

The eval was rebuilt around metrics that reflect the actual business question:
*"Does the cheaper model make the same surface/reject decisions as the expensive
one?"*

| Metric | What it catches |
|---|---|
| **Signed bias** `mean(haiku − sonnet)` | Systematic over/under-scoring; positive = Haiku over-scores |
| **Distribution** (higher / lower / equal count) | Whether errors are symmetric noise or a directional shift |
| **Corridor analysis** `[50, 70]` | Separate metrics for vacancies near the threshold, where errors are costly |
| **Agreement rate** | Fraction of vacancies where both models make the same surface/reject call |
| **Confusion matrix** | false-reject (good vacancy missed), false-surface (junk surfaced) |

Green verdict requires all three: `|bias| < 8` **and** `corridor false-reject = 0`
**and** `corridor false-surface = 0`.

MAE and agreement rate are still shown in the output but are not gate criteria
on their own. A model can have 100% agreement and a bias of +20 on vacancies
that happen to all land on the same side of the threshold — agreement catches
this, MAE doesn't.

### Sampling strategy

The first eval sampled the 30 most recent vacancies. Most were confidently
rejected (score < 40). The threshold corridor [50–70] was barely represented,
so the eval never tested the decision boundary it was supposed to protect.

Fixed with stratified sampling: up to 8 vacancies per score band
(`< 40`, `40–59`, `60–75`, `> 75`), guaranteeing the corridor is always covered.

### Fix 1: calibration via few-shot anchoring

Bias root cause: without reference points, Haiku interprets the rubric more
leniently. Fix: add four calibration anchors to the scoring prompt, each with a
concrete score tied to the real candidate profile:

```
~10  — backend developer, RF office, no LLM/AI, mandatory degree
~42  — data scientist (model training), livecoding interview, language gap
~65  — LLM/prompt engineer, remote, right stack, but hard seniority gap
~88  — AI/prompt engineer, EU relocation/remote, exact profile match, no blockers
```

Result: bias dropped from **+13 → +1.6**. The anchors gave Haiku a calibrated
scale without changing the judgment criteria.

### Fix 2: confidence-based routing

Calibration fixed bias. Residual variance near the threshold caused 2 false-rejects
in the corridor: Haiku incorrectly rejected vacancies that Sonnet would surface.
Calibration cannot fix variance — scatter around the truth is inherent to a
weaker model's uncertainty.

Fix: **route by confidence**, not by surface/reject outcome.

```
haiku_score < 50    →  confident reject  →  Haiku score final, no second call
haiku_score 50–70   →  uncertainty zone  →  judge re-scores; judge's score FINAL
haiku_score > 70    →  confident surface →  Haiku score final; judge refines
                                            Обоснование text only (quality)
```

The corridor `[50, 70]` is intentionally wider than the threshold `60` in both
directions — it catches vacancies Haiku might mis-score on either side of the
decision boundary.

Fallback: if the judge call fails inside the corridor, Haiku's score is used.
The vacancy is never dropped due to a second-call timeout.

### Offline vs. online coverage

The eval runs offline against stored Sonnet scores. It validates the model layer
(calibration, agreement) but cannot see the runtime routing logic, because the
corridor re-score is a new live call that doesn't exist in historical data.

Runtime routing is monitored through structured log lines:

```
[score] id=N haiku=X → corridor → judge=Y final=Y
[score] id=N haiku=X final=X (no corridor)
[score] id=N haiku=X → corridor → judge FAILED (...) fallback=X
```

Offline eval + online log coverage together give full observability.

---

## Case 2: DeepSeek migration (planned)

*Placeholder — to be filled after migration.*

Plan: run the same eval script with DeepSeek as the cheap model, comparing
against the stored Sonnet baseline. Pass criteria: `|bias| < 8`,
`corridor false-reject = 0`, `corridor false-surface = 0`.

See [GitHub issue #TBD](https://github.com/dontkaiad/job-hunter-agent/issues)
for migration steps and gotchas.

---

## Metric cheat-sheet

| Metric | What it catches | When it misleads |
|---|---|---|
| **MAE** | Average error magnitude | Blind to direction: bias of +13 looks the same as symmetric ±13 noise |
| **Signed bias** | Systematic over/under-scoring | Doesn't see variance: zero bias with high scatter still flips decisions |
| **Agreement rate** | Same surface/reject calls overall | Misses *which* items disagree; a corridor false-reject counts the same as a junk disagreement |
| **Corridor false-reject/surface** | Decision errors near the threshold, where they hurt | Doesn't catch errors on obvious cases (both < 40 or both > 75) — but those don't matter for the product |

The right metric depends on what the model output is used for. For a binary
surface/reject decision, corridor decision accuracy is the primary signal.
Score proximity is secondary.
