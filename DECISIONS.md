# Decisions

Short, ADR-style records of the key AI-integration decisions: the context, the
decision, and why. They document *why the system is built the way it is*, which
matters more than the code for understanding the engineering.

---

## 1. Cost-aware model routing — Sonnet only for judgment

**Context.** Every posting is processed end-to-end with LLMs. Using a frontier
model for all of it (extract, score, research, draft) would be simple but
needlessly expensive; using a cheap model for everything would compromise the
one step that requires real judgment.

**Decision.** Route by what each step actually needs. The three high-volume /
mechanical steps — extract, research, draft — run on **Claude Haiku 4.5**. The
single judgment step — relevance **scoring** — runs on **Claude Sonnet 4.6**.

**Why.** Scoring is where model quality changes the outcome (a nuanced
HARD-vs-SOFT requirement read, a defensible rationale); extraction and drafting
are structured transforms a cheaper model handles well. The numbers bear it out:
a Sonnet score costs ≈ **$0.0063/posting** vs a Haiku extract ≈ **$0.0033**, and
moving extract to Sonnet would roughly **3×** that step's cost for no quality
gain. Sonnet is already ~⅔ of the daily harvest cost from just one of two
per-posting steps — so it is spent only where it earns its keep. (Full math in
[COST.md](COST.md).)

---

## 2. Source-grounded research with an explicit honesty model

**Context.** An application drafted from hallucinated "company facts" is worse
than useless — it's embarrassing in front of a recruiter. But many postings have
no fetchable company page.

**Decision.** Research grounds facts in **fetched page text only**, returned as
`sourced_facts`. The result carries a `research_source` flag: `"web"` when a real
page was fetched, `"desk_fallback"` when it could not be. On `desk_fallback`,
`sourced_facts` is emptied — the model may still produce talking points and
questions, but it is structurally prevented from presenting invented facts as
confirmed.

**Why.** It makes the honesty boundary explicit and machine-checkable rather than
a hope pinned on a prompt. The draft can lean on confirmed facts when they exist
and stay appropriately generic when they don't, and the operator can see which
mode produced any given draft.

---

## 3. HARD vs SOFT requirements in scoring

**Context.** Job postings mix mandatory requirements with wish-list items.
Treating every listed requirement as a gate would reject strong-fit roles over a
single "nice-to-have" the candidate happens not to have.

**Decision.** The scoring rubric classifies each requirement by the posting's own
wording: **HARD** (`required` / `must` / `обязательно` / …) vs **SOFT**
(`preferred` / `nice to have` / `будет плюсом` / …). A missing HARD requirement
flags and lowers the score; a missing SOFT requirement only *lightly* lowers it
and is phrased as a mild caveat, never a rejection risk. Ambiguous wording
defaults to SOFT.

**Why.** It mirrors how a sensible human reads a posting and prevents the
high-recall failure of discarding good matches on optional gaps — while still
surfacing genuine blockers. The classification logic lives in the prompt
scaffold (code), and the candidate-specific data is injected from the profile;
neither is hardcoded.

---

## 4. Graceful fallback everywhere — the pipeline never blocks

**Context.** The pipeline depends on external services that fail intermittently:
the LLM API, web fetches, FX rate providers.

**Decision.** Every external step has a defined degradation, never a hard stop.
Extract falls back to a regex heuristic if the LLM response won't parse. Research
falls back to desk mode if the fetch fails or is blocked. The salary guard
simply doesn't fire if FX is unavailable. A failed non-critical send (e.g. the
run summary) is swallowed so it can't skip the heartbeat or crash the harvest.

**Why.** A job-hunting assistant that stalls on a flaky page fetch is useless. A
degraded-but-moving pipeline is strictly better than a blocked one, and the
`research_source` flag (Decision 2) keeps the degradation visible rather than
silent.

---

## 5. Prompt caching only where it actually engages

**Context.** Anthropic prompt caching only activates when the cached prefix
clears a model-specific minimum (~1,024 tokens for Sonnet, ~2,048 for Haiku).
Below that, `cache_control` is ignored — adding it is just request noise.

**Decision.** Measure each constant system prompt and tag it for caching **only**
when it clears its model's floor. In practice the Sonnet scoring system prompt
(~1,422 tokens) **is** cached; the three Haiku system prompts (extract ~981,
research ~380, draft ~1,404) are all below Haiku's 2,048 floor and are correctly
**not** tagged.

**Why.** Caching the repeated Sonnet scoring prefix cuts it from full input cost
to a 10% cache-read on every posting after the first in a run (~26% off the daily
harvest). Tagging the sub-threshold prompts would do nothing but add noise — so
the code makes the judgment per prompt (`should_cache_system`) instead of
blanket-tagging. Spending discipline includes *not* performing rituals that don't
pay.

---

## 6. `max_tokens` tuned per step to avoid silent truncation

**Context.** A response cut off at `max_tokens` mid-JSON has no closing brace and
won't parse — which then *silently* degrades the caller (extract → heuristic,
score → fallback, research → empty). This was a real, confirmed bug.

**Decision.** Set per-step output caps from each step's realistic worst-case
output, with headroom: extract 1,200, score 800, research 2,000 (it carries
fetched page context), draft 500.

**Why.** Output is billed only for tokens actually emitted, so a generous cap on
a normally-short response costs nothing — it's pure safety margin against
truncation. The caps are sized to remove the truncation failure without paying
for headroom that's never used.

---

## 7. Human-in-the-loop by design

**Context.** The end action is sending a real application to a real company about
the operator's own career. The cost of a bad autonomous send is high and
personal; the cost of a missed surfacing is low (it waits in a queue).

**Decision.** The agent **proposes** (surfaces scored matches, drafts
applications); the human **disposes** (Approve / Backlog / Skip, then Send).
Expensive and irreversible steps sit behind human gates: research + draft run
only after Approve, and nothing is sent without an explicit Send tap.

**Why.** HITL puts judgment and accountability where they belong for a
high-stakes, personal task, and it doubles as a cost lever — the heavy
research/draft calls run on the handful of approved roles per day, not on the
whole backlog. The automation removes the tedium (reading the firehose, parsing,
scoring, first-draft writing) while the person keeps the decisions that matter.
