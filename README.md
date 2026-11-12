# job-hunter-agent

> An AI agent that ingests job postings, scores them against a candidate profile
> with explainable reasoning, researches the company, and drafts tailored
> applications — with a human in the loop at every gate.

**The problem.** Job hunting is tedious in any field: reading a firehose of
postings — most of them irrelevant — and then writing a fresh, tailored
application for each promising one.

**The solution.** This agent harvests postings, extracts each into a structured
schema, and scores role-fit with a rubric-driven LLM judge that returns a
human-readable rationale. Only for the roles the operator *approves* does it run
source-grounded company research and draft an application. Nothing is ever sent
automatically: a human approves at the surface gate and again before send.

## Features

- **Explainable AI scoring** — a rubric-driven Claude Sonnet judge returns a
  0–100 relevance score plus a short reasoning verdict shown on every card. It
  classifies each posting requirement as HARD or SOFT, so a missing
  "nice-to-have" only lightly lowers the score instead of sinking a good-fit role.
- **Source-grounded research** — fetches the company page and grounds claims in
  the fetched text (`sourced_facts`). When no real page is available it falls
  back to "desk" research and labels itself (`research_source`), never
  fabricating company facts.
- **Human-in-the-loop pipeline** — a Telegram bot (inline Approve / Backlog /
  Skip) and a read/act React dashboard drive the *same* state machine. The agent
  proposes; the human disposes.
- **Borderline band** — postings scoring 40–49, just below the surface
  threshold (T = 50), are listed in a dedicated review view so near-misses are
  examined, not silently dropped.
- **Pluggable sources** — one `Source` interface behind several feeds: public
  Telegram channels (a plain web reader, or Telethon as an option), the Jobicy
  API, We Work Remotely RSS, Habr Career, the monthly Hacker News "Who is
  hiring" thread, and add-by-URL for any single vacancy. Each non-Telegram
  source is opt-in through the environment (see `.env.example`).
- **Market analysis** — a dashboard page built from the collected pool: salary
  benchmark (currencies converted, pay period guessed by a labelled
  heuristic), stack, grade and benefits frequencies.
- **Competencies** — a hand-curated skill inventory with code-level evidence
  per skill, crossed with how often the market asks for each skill.
- **Cost-aware model routing** — Haiku for the high-volume steps and the first
  scoring pass; Sonnet only as the judge when the Haiku score falls in the
  40–60 corridor; prompt caching where it actually engages. Measured cost per
  call is in [COST.md](COST.md).

## Tech stack

- **Agent / backend:** Python 3.9, aiogram (Telegram bot), APScheduler (daily harvest)
- **Dashboard:** FastAPI + uvicorn serving a React (Vite) SPA, Telegram-login auth
- **Data:** PostgreSQL (psycopg 3)
- **AI:** Anthropic API — Claude **Haiku 4.5** (extract / research / draft) and
  Claude **Sonnet 4.6** (the scoring judge)
- **Infra:** Docker Compose on a single VPS, Caddy reverse proxy + TLS
- **External:** live, no-key FX (open.er-api.com, with frankfurter as the fallback) for salary normalization

> **Roadmap (not yet built):** further job sources beyond the ones listed
> above; the cost model in [COST.md](COST.md) still lacks a measured daily
> total (it needs real ingest volume and the share of postings that reach the
> judge corridor).

## Architecture in brief

A deterministic state machine moves each posting through
`ingest → extract → score → surface → (human) → research → draft → sent`. A
single function — `advance()` — is the **only** writer of item state, so every
transition is consistent and auditable; the automated harvest path and the
human-action path both go through that one writer. Full design, data model,
security model, and scope choices are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Cost

Measured from real API responses (`python -m evals.cost_probe`, 2026-11-01): one
ordinary posting costs about **$0.0097** for extract + the Haiku first pass
(≈ 2.1k + 4.5k input tokens, ≈ 0.6k output), and about **$0.0229** when the
Sonnet judge also runs. The daily bill is the ingest volume times that mix; no
daily total is claimed, because the corridor share is not recorded. The older
modelled figures (including a per-day figure) are superseded and kept in
[COST.md](COST.md) only as history, together with the per-step token math and
the cost-aware design levers.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — system design, state machine, data model, security, observability, scope choices
- [DECISIONS.md](DECISIONS.md) — ADR-style record of the key AI-integration decisions
- [COST.md](COST.md) — real cost numbers and the cost-aware design
- [docs/CASES/](docs/CASES/README.md) — evals and a postmortem, each with methodology, n, results, limitations, reproduction and cost. Reproduce offline with `python -m evals offline`:
  - [0001](docs/CASES/0001-score-stability-temperature.md) score stability (temperature 0)
  - [0002](docs/CASES/0002-parse-robustness-text-vs-tool.md) parse robustness (forced tool use)
  - [0003](docs/CASES/0003-postmortem-2026-10-credit-outage.md) the 2026-10 credit-outage postmortem
  - [0004](docs/CASES/0004-link-only-fetch.md) link-only fetch
  - [0005](docs/CASES/0005-heuristic-vs-model-extract.md) heuristic vs model extract
  - [0006](docs/CASES/0006-golden-set-and-geo-guard.md) golden set (precision / recall of the score) and the geo guard
  - [0007](docs/CASES/0007-judge-model-sonnet-4-6-vs-5-5.md) score judge: Sonnet 4.6 vs 5.5 (a "not worse" check; keep 4.6)
- `DESIGN.md` / `SCORING.md` — the authoritative low-level spec

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                                       # fill in values
cp config/profile.example.yaml config/profile.local.yaml   # your profile (gitignored)
.venv/bin/python -m pytest                                 # all I/O mocked; no creds needed
```

The candidate profile is a **gitignored** YAML; in its absence the bot falls
back to the generic, runnable `config/profile.example.yaml`. The repo ships
**no personal data** — the dashboard profile defaults to a generic "Кандидат"
with an initials placeholder, and the real name/avatar are supplied only as
out-of-repo build inputs at deploy time.

Run locally:

```bash
.venv/bin/python -m job_hunter.run     # one-shot: ingest → score → deliver surfaced cards
.venv/bin/python -m job_hunter.serve   # long-running: handle button taps + the daily 10:00 harvest
```

Deployment is two Docker Compose services (the bot and the dashboard) on a VPS
behind Caddy; see the Deployment section of [ARCHITECTURE.md](ARCHITECTURE.md).
