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

![Dashboard](docs/dashboard.png)
<!-- Drop a de-identified dashboard screenshot at docs/dashboard.png (see docs/README.md). -->

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
- **Borderline band** — postings scoring 50–59 are surfaced in a dedicated
  review view so near-misses are examined, not silently dropped.
- **Cost-aware model routing** — Haiku for the high-volume steps, Sonnet
  reserved for the single judgment step, prompt caching where it actually
  engages. Steady-state ≈ **$0.13 per daily harvest** (see [COST.md](COST.md)).

## Tech stack

- **Agent / backend:** Python 3.9, aiogram (Telegram bot), APScheduler (daily harvest)
- **Dashboard:** FastAPI + uvicorn serving a React (Vite) SPA, Telegram-login auth
- **Data:** PostgreSQL (psycopg 3)
- **AI:** Anthropic API — Claude **Haiku 4.5** (extract / research / draft) and
  Claude **Sonnet 4.6** (the scoring judge)
- **Infra:** Docker Compose on a single VPS, Caddy reverse proxy + TLS
- **External:** live, no-key FX (frankfurter.app / open.er-api.com) for salary normalization

> **Roadmap (not yet built):** the Competencies view (currently a stub), the
> Market analysis view (currently a stub), and support for additional job
> sources beyond the current channels.

## Architecture in brief

A deterministic state machine moves each posting through
`ingest → extract → score → surface → (human) → research → draft → sent`. A
single function — `advance()` — is the **only** writer of item state, so every
transition is consistent and auditable; the automated harvest path and the
human-action path both go through that one writer. Full design, data model,
security model, and scope choices are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Cost

Steady-state ≈ **$0.13 per daily harvest** (~13 postings, extract + score) and
≈ **$0.01 per approved vacancy** (research + draft). The full method, per-step
token math, and the four cost-aware design levers are in [COST.md](COST.md).

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — system design, state machine, data model, security, observability, scope choices
- [DECISIONS.md](DECISIONS.md) — ADR-style record of the key AI-integration decisions
- [COST.md](COST.md) — real cost numbers and the cost-aware design
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
