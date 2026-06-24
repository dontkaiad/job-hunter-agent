# Cost

This project is built to be cheap to run. This document shows the real per-step
costs, the daily and per-approval math, and the four design levers that keep it
low.

![Daily cost](docs/cost.png)
<!-- Drop the daily-cost chart screenshot at docs/cost.png (see docs/README.md). -->

## Method & honesty caveat

The dollar figures below are **modeled** from the actual code constants
(per-step model routing, `max_tokens`, and the prompt-caching decisions in
`job_hunter/llm.py`) multiplied by current Anthropic list prices. Token counts
were measured by running the repo's own prompt builders and token estimator over
a representative ~830-character Cyrillic posting and a 6,000-character fetched
page (the real per-page research cap).

They are **not** drawn from billing telemetry: the repo intentionally logs no
per-call token usage, so these are estimates, not invoice line items. Stating
this plainly is the honest framing — the numbers are a faithful model of the
code, accurate to within the variance of real posting size and model output
length.

**Prices (Anthropic API, standard tier, per million tokens):**

| Model | Input | Output | Cache read | Cache write (5-min) |
|---|---|---|---|---|
| Claude Haiku 4.5 | $1.00 | $5.00 | $0.10 | $1.25 |
| Claude Sonnet 4.6 | $3.00 | $15.00 | $0.30 | $3.75 |

Source: [Anthropic API pricing](https://platform.claude.com/docs/en/about-claude/pricing)
(corroborated by [CloudZero](https://www.cloudzero.com/blog/claude-api-pricing/)
and [Finout](https://www.finout.io/blog/anthropic-api-pricing), June 2026).

## Per-step model + token map

Read from the code; input tokens measured on the representative posting. Ingest
and the prefilter are deterministic (no LLM, no cost).

| Step | Model | Input tokens (sys + user) | Output cap | System cached? |
|---|---|---|---|---|
| extract | Haiku 4.5 | ≈ 1,585 (sys 981 + user ~600) | 1,200 | No — sys 981 < Haiku's 2,048 floor |
| score | Sonnet 4.6 | ≈ 1,890 (sys **1,422 cached** + user ~470) | 800 | **Yes** — 1,422 > Sonnet's 1,024 floor |
| research | Haiku 4.5 | ≈ 2,360 (sys 380 + user ~1,980 w/ 6k page) | 2,000 | No — sys 380 < 2,048 |
| draft | Haiku 4.5 | ≈ 1,945 (sys 1,404 + user ~540) | 500 | No — sys 1,404 < 2,048 |

Output caps are safety margins against mid-JSON truncation; billing is only for
tokens actually emitted, so the figures below use realistic typical outputs.

## Cost of one normal daily harvest (~13 postings)

Each posting runs extract (Haiku) + score (Sonnet). Per posting:

- **Extract (Haiku):** in 1,585 × $1/M + out ~350 × $5/M = **$0.0033**
- **Score (Sonnet, cache hit):** cached-system read 1,422 × $0.30/M
  + user 470 × $3/M + out ~300 × $15/M = **$0.0063**
  (the first score call of the run pays a one-time cache *write* of
  1,422 × $3.75/M ≈ $0.0053 instead of the $0.0004 read)

Daily total for 13 postings:

- Extract: 13 × $0.0033 = **$0.043**
- Score: 1 cache-write call ($0.0112) + 12 cache-read calls (12 × $0.0063 = $0.076) = **$0.087**
- **≈ $0.13 per harvest day → ~$3.9 / month** for the bulk pipeline.

(The 13 score calls run seconds apart, well inside the 5-minute cache TTL, so the
warm-cache assumption holds.)

## Cost of one approval (research + draft)

Research and draft run **only after a human Approve**, on the ~2–4 roles approved
per day — not on the whole backlog.

- **Research (Haiku, with a 6k-char page):** in 2,360 × $1/M + out ~800 × $5/M = **$0.0064**
- **Draft (Haiku):** in 1,945 × $1/M + out ~300 × $5/M = **$0.0034**
- **≈ $0.01 per approved vacancy.**

**All-in steady state:** ~$0.13 harvest + ~3 approvals × $0.01 ≈ **$0.16 / day
(~$4.8 / month).**

## "But the first 4 days cost ~$8"

That was development, not steady state. Every change to the profile, the scoring
rubric, or the research prompt re-ran the **entire ~60-card backlog** through
extract + score to see the effect.

- One full 60-card re-harvest: extract 60 × $0.0033 ($0.20) + score
  (1 write + 59 reads) ($0.0112 + 59 × $0.0063 = $0.384) ≈ **$0.59 per pass**.
- ~$8 ÷ ~$0.59 ≈ **~13 full re-harvests** (or fewer, plus research/draft
  experiments) — exactly what tuning prompts and the rubric over four days looks
  like.

Steady state for the same four days would be ~$0.5–0.65 total. The ~$8 is a
**development burst (~15× steady state)**; the live cost going forward is
≈ $0.13–0.16 / day.

## Cost-aware design (four levers, each with its number)

1. **Tiered model routing.** Sonnet is reserved for the one judgment step
   (scoring). It already accounts for ~⅔ of the daily harvest cost from one of
   two per-posting steps; moving extract to Sonnet too would ~3× that step
   ($0.0033 → ~$0.010/posting, +$0.087/day). The three mechanical steps stay on
   Haiku.

2. **Prompt caching where it engages.** The Sonnet scoring system prompt
   (~1,422 tokens) clears Sonnet's 1,024-token floor and is cached, turning the
   repeated prefix from $0.0043 (full input) into $0.0004 (read) per posting
   after the first — ~26% off the harvest day. The three Haiku prompts are below
   Haiku's 2,048 floor, so they are deliberately **not** tagged (it would be a
   no-op).

3. **`max_tokens` tuned per step** (extract 1,200 / score 800 / research 2,000 /
   draft 500). Sized to prevent the mid-JSON truncation that silently degrades a
   step, without paying for unused headroom — output is billed only for tokens
   emitted, so the caps cost nothing on a normal response.

4. **Research + draft only post-approve.** The heavier Haiku calls (research
   carries ~2,360 input tokens with the 6k-char page) fire on the ~2–4 approved
   roles per day, never on all ~60 surfaced/backlog items — keeping the
   per-approval cost (~$0.01) off the daily-harvest bill.
