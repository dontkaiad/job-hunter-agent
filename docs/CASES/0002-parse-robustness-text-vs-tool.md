# Case 0002 — Parse robustness: text JSON parsing vs forced tool use

- **Status:** Accepted (shipped in `406723e`, 2026-11-01)
- **Date:** 2026-11-01
- **Code conforms to:** `job_hunter/llm.py` (`SCORE_TOOL`, `parse_score_tool_input`,
  `parse_score_response`, `AnthropicClient.complete_tool`), `job_hunter/pipeline.py`
  (`_park_item`), `evals/parse_robustness_eval.py`
- **Baseline:** [`evals/results/parse_robustness_2026-11-01.json`](../../evals/results/parse_robustness_2026-11-01.json)

## Summary

On 2026-11-01 the harvest alert fired: 12 of 46 scoring calls failed, all with
error type `parse` (the previous day's background was ~2.5 %). Re-running the 12
vacancies showed it was **not** truncation and **not** a markdown wrapper: the model
answered in prose ("I cannot access external URLs…") because the post was a bare
link. Scoring now uses a forced tool call (`submit_score`), so prose is impossible
by construction, plus a deterministic `link_only` gate and an `insufficient_content`
flag so a contentless post is parked instead of failed or guessed.

## Methodology

- **What was measured:** for the 12 deferred vacancies, (a) the break type of the
  reply on the old text path, (b) the outcome on the new path.
- **How:** same prompt and model (`claude-haiku-4-5`) replayed on production data,
  read-only, by `evals/parse_robustness_eval.py run`. Text path: `max_tokens=800`,
  free text → regex JSON extraction, default temperature. Break types: *truncated*
  (`stop_reason=max_tokens` or unbalanced braces), *markdown wrapper*, *prose / no
  JSON*, *invalid escape*, *other*. New path: the link-only gate, then the
  forced-tool call (`temperature=0`), with `insufficient_content` / null score parked.
- **Sample:** n = 12 — the vacancies the harvest deferred. 9 had been deferred the
  day before as well; 3 were new.
- **Output kept:** counts by type, input lengths, `stop_reason`. Raw replies are not
  stored; only first/last ~100 characters of four refusals are kept as a test
  fixture (`evals/synthetic/score_prose_refusals.json`) — no profile detail.
- **Two replays exist:** an ad-hoc one the same morning (run 1) and the committed
  baseline from the script (run 2). They differ; both are reported.

## Results

Old text path, replay of the 12 (committed baseline, run 2):

| Outcome | Count | Notes |
|---|---|---|
| Prose refusal, no JSON | **6** | inputs of **83, 83, 83, 112, 129, 131 characters**; `stop_reason=end_turn` |
| Parsed OK | 6 | inputs of 115, 120, 176, 273, 281 and 6070 chars |
| Truncated by `max_tokens` | **0** | `stop_reason=max_tokens` never seen |
| Markdown-wrapped / invalid escape / other | **0** | |

Run 1 gave 9 refusals / 3 OK. **The count of refusals did not reproduce (9 vs 6),**
and two inputs that refused in run 1 (273 and 281 chars) parsed in run 2. This is the
expected behaviour of free-text answers at the default temperature: for a given stub
the model refuses *sometimes*. What did reproduce in both runs: every failure was a
**prose refusal on a short stub input (≤ 281 chars in run 1, ≤ 131 in run 2)**, none
was truncation or formatting, and the 6070-character vacancy — which had failed in
the production harvest — parsed both times. So the dominant cause is a contentless
input producing prose, not several formatting problems; its *rate* on a given stub is
not stable.

New path, same 12 (committed baseline):

| Outcome | Count |
|---|---|
| `link_only` gate — no LLM call, no attempt spent | 8 |
| `insufficient_content` — parked, no attempt spent | 4 |
| Scored | 0 |
| **Parse failures** | **0** |

Run 1 of the tool path had 3 insufficient + 1 scored (one vacancy scored 15). Run 1
used default temperature and predates the `temperature=0` change; run 2 is at
`temperature=0`, and that vacancy is now classified `insufficient_content`. The
`insufficient_content` boundary is therefore temperature-sensitive for borderline
inputs — one flip in 4 non-gated cases, which is a small observation, not a rate.

For reference the old behaviour on these inputs was: each failed parse cost one of
three attempts, and nine vacancies were at 2/3 — one more harvest from `score_failed`.

## Limitations

- **n = 12, one day, one model on the text path.** The counts moved between two
  replays (9 → 6 refusals), so the break-type shares are not stable estimates; the
  finding is the qualitative one (all text-path failures here were prose refusals on
  short stub inputs).
- **"0 parse failures" is by construction, not a quality result.** A forced tool call
  cannot return prose. The metric that matters next is whether the *scores* are
  right, and that was **not measured** — there is no golden set. In particular
  `insufficient_content` decisions (4 of 12) were not audited against a human
  judgement of "is there enough content".
- Truncation and markdown wrappers did not occur in this sample, so this eval does
  not demonstrate that the tool path fixes them. They are covered only by synthetic
  replies in the offline harness, where the old parser already salvages a fenced
  reply and (correctly) cannot salvage a truncated one.
- The replay re-queried the model; it is not the original harvest reply.

**Next step:** an accuracy eval against the operator's recorded approve / skip
decisions (see Case 0001), which would also let us audit `insufficient_content`.

## Cost

The live runs of Cases 0001–0004 predate the per-call usage tagging (`llm_usage.step = 'eval:…'`, introduced 2026-11-08), so their cost was not measured separately. A ceiling of about **$0.40** was agreed for the live runs; none of them is re-run in CI (`python -m evals offline` makes no API call).

## Reproduce

Offline (synthetic break types, saved refusals, tool-path parking; runs in CI):

```bash
python -m evals offline
```

Live replay on specific vacancies (read-only DB, paid API, **≈ $0.05** for 12:
~16 Haiku calls at [COST.md](../../COST.md) rates — an estimate, not metered):

```bash
python -m evals.parse_robustness_eval run --max-usd 0.3 --save my_run.json --ids 11009 11063 11085 11154 11211 11345 11349 11351 11353 11466 11484 11488
```
