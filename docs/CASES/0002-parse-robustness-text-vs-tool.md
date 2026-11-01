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
  read-only. Text path: `max_tokens=800`, free text → regex JSON extraction. Break
  types: *truncated* (`stop_reason=max_tokens` or unbalanced braces), *markdown
  wrapper*, *prose / no JSON*, *invalid escape*, *other*. New path: the link-only
  gate, then the forced-tool call, with `insufficient_content` / null score parked.
- **Sample:** n = 12 — the vacancies the harvest deferred. 9 had been deferred the
  day before as well; 3 were new.
- **Output kept:** counts by type, input lengths, `stop_reason`. Raw replies are not
  stored; only first/last ~100 characters of four refusals are kept as a test
  fixture (`evals/synthetic/score_prose_refusals.json`) — they contain no profile
  detail.

## Results

Old text path, replay of the 12:

| Outcome | Count | Notes |
|---|---|---|
| Prose refusal, no JSON | **9** | every one an input of **83–281 characters** (a link + a label); `stop_reason=end_turn` |
| Parsed OK | 3 | inputs of 120, 176 and 6070 chars |
| Truncated by `max_tokens` | **0** | |
| Markdown-wrapped | **0** | |
| Invalid escape / other | **0** | |

So one dominant cause (a contentless input), not several formatting problems. The
three that parsed show the failure is also stochastic at the default temperature:
the 6070-character vacancy had failed in the production harvest and parsed in the
replay — the "random refusal on a long text" case. (The second and third parsed
replies were short link stubs that happened not to refuse this time.)

New path, same 12:

| Outcome | Count |
|---|---|
| `link_only` gate — no LLM call, no attempt spent | 8 |
| `insufficient_content` — parked, no attempt spent | 3 |
| Scored | 1 |
| **Parse failures** | **0** |

For reference the old behaviour on these inputs was: each failed parse cost one of
three attempts, and nine vacancies were at 2/3 — one more harvest from `score_failed`.

## Limitations

- **n = 12, one day, one model on the text path.** The break-type shares are not
  stable estimates; the finding is the qualitative one (all text-path failures here
  were prose refusals on stub inputs).
- **"0 parse failures" is by construction, not a quality result.** A forced tool call
  cannot return prose. The metric that matters next is whether the *scores* are
  right, and that was **not measured** — there is no golden set. In particular
  `insufficient_content` decisions (3 of 12) were not audited against a human
  judgement of "is there enough content".
- Truncation and markdown wrappers did not occur in this sample, so this eval does
  not demonstrate that the tool path fixes them. They are covered only by synthetic
  replies in the offline harness, where the old parser already salvages a fenced
  reply and (correctly) cannot salvage a truncated one.
- The replay re-queried the model; it is not the original harvest reply.

**Next step:** an accuracy eval against the operator's recorded approve / skip
decisions (see Case 0001), which would also let us audit `insufficient_content`.

## Reproduce

Offline (synthetic break types, saved refusals, tool-path parking; runs in CI):

```bash
python -m evals offline
```

Live replay on specific vacancies (read-only DB, paid API, **≈ $0.05** for 12:
~16 Haiku calls at [COST.md](../../COST.md) rates — an estimate, not metered):

```bash
python -m evals.parse_robustness_eval run --ids 11009 11063 11085 11154 11211 11345 11349 11351 11353 11466 11484 11488
```
