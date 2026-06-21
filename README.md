# job-hunter-agent

Event-driven agent that scores AI/LLM job posts and drafts applications,
human-in-the-loop at every gate.

See `DESIGN.md` (state machine, schema, module layout, extract schema) and
`SCORING.md` (scoring rules) for the authoritative spec.

## Pipeline

```
discovered → extracted → scored → { rejected | surfaced }
surfaced  → { skipped | backlog | approved }
approved  → researched → drafted → sent → closed
```

- **extract (T1)** — LLM (`claude-haiku-4-5`) parses raw posts into the Extract
  schema; deterministic regex heuristics are the fallback.
- **score (T2–T4)** — deterministic rules (`SCORING.md`). Salary is converted to
  a ₽-equivalent via live no-key FX (frankfurter.app / open.er-api.com, cached
  24h) for the < 150k ₽ hard reject and for display. Near the surface threshold
  T=60, an LLM tiebreak makes the final call.
- **surface gate (T5–T9)** — aiogram bot sends the job with inline buttons
  (Approve / Backlog / Skip) that drive `advance(item, decision=...)`.
- **research (T10) / draft (T11)** — LLM-backed; draft is reviewed with a Send
  button (T12), then archived (T13).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in the values (see below)
# Your candidate profile (role, skills, salary floor, draft signature, ...) is a
# YAML file, NOT an env var. Copy the generic example and edit it:
cp config/profile.example.yaml config/profile.local.yaml   # then fill in your details
```

`config/profile.local.yaml` is **gitignored** and holds your personal data — it
is never committed. If it is absent the bot falls back to the generic, runnable
`config/profile.example.yaml`. The profile drives the scoring rubric block, the
draft prompt (gender / honesty / signature), and the EUR/month salary floor.

## .env keys

| Key | What | Where to get it |
|-----|------|-----------------|
| `ANTHROPIC_API_KEY` | LLM for extract/tiebreak/research/draft | console.anthropic.com → API Keys |
| `ANTHROPIC_MODEL` | LLM model (default `claude-haiku-4-5`) | — |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | Telethon userbot app | my.telegram.org/apps |
| `TELEGRAM_SESSION` | StringSession (run the login command below) | generated locally |
| `TELEGRAM_SESSION_NAME` | fallback on-disk session file name | — |
| `TELEGRAM_CHANNELS` | comma-separated source channels (`@a,@b`) | the channels you read |
| `TELEGRAM_FETCH_LIMIT` | messages per channel per run | default 50 |
| `BOT_TOKEN` | aiogram notification bot token | @BotFather |
| `NOTIFY_CHAT_ID` | numeric chat to notify | @userinfobot |
| `DB_PATH` | SQLite path (default `job_hunter.db`) | — |
| `FX_PROVIDER` | `frankfurter` or `erapi` | — |
| `FX_CACHE_TTL` | FX cache seconds (default 86400) | — |

## Run

The system runs as **two cooperating processes** that share the SAME SQLite DB
(`DB_PATH`, default `job_hunter.db`):

| Command | Lifetime | What it does |
|---------|----------|--------------|
| `python -m job_hunter.run` | one-shot (exits) | harvest → extract → score → **deliver** surfaced cards (with Approve / Backlog / Skip buttons) to the bot chat. Cron-friendly. |
| `python -m job_hunter.serve` | long-running | starts aiogram long-polling and **receives the button taps**, driving `advance()`: Approve / Backlog / Skip at the surface gate and draft → Send at the draft gate. |

Because `run` exits immediately, it cannot receive the `callback_query` updates
produced when you tap a button — so the buttons would appear to do nothing on
their own. `serve` is the long-lived half that handles those taps. They talk
through the shared DB: `run` writes the surfaced cards, `serve` reads each tap
and advances the item. Run `run` on a schedule (or by hand) to deliver new
cards; keep `serve` running to action them.

Both need `BOT_TOKEN` + `NOTIFY_CHAT_ID`. The **Approve → research → draft** path
additionally needs `ANTHROPIC_API_KEY` (when you tap Approve, `serve` drives the
LLM research + draft steps and sends the generated отклик back with a Send
button); without it, Approve still advances the state but no draft is produced.

```bash
# 1) one-time: generate a reusable Telegram StringSession (interactive)
#    ONLY needed for INGEST_MODE=telethon; the default web ingest needs no auth.
.venv/bin/python -m job_hunter.ingest_telegram --login   # paste into TELEGRAM_SESSION

# 2) one-shot: ingest + score + deliver surfaced cards to the bot chat
.venv/bin/python -m job_hunter.run

# 3) long-running: handle button presses (approve/backlog/skip, draft→send).
#    Keep this alive; Ctrl-C stops it cleanly (HTTP session + DB closed).
.venv/bin/python -m job_hunter.serve

# ingest only (no scoring/notify)
.venv/bin/python -m job_hunter.ingest_telegram
```

> Note: `python -m job_hunter.bot` is an alias of `python -m job_hunter.serve`
> (same startup → polling → graceful-teardown lifecycle).

## Tests

```bash
.venv/bin/python -m pytest
```

All LLM, FX and Telegram I/O is mocked in tests; no network/credentials needed.
