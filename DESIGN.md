# job-hunter-agent — Core Design (SECRET-FREE)

Event-driven agent that scores AI/LLM job posts and drafts applications,
human-in-the-loop at every gate.

This document specifies the **secret-free core** only. It contains
**no Telegram auth, no API keys, and no network calls**. All I/O is local
SQLite. LLM steps, userbot reads, drafting, and enrichment are stubbed
behind interfaces and explicitly deferred (see §6).

**Hard constraints**

- Python 3, stdlib `sqlite3` only, `pytest` for tests. No frameworks.
- **Timezone-aware datetimes ONLY**: always `datetime.now(timezone.utc)`.
  Bare `datetime.now()` / `utcnow()` are forbidden everywhere.
- Pure logic (no I/O, no clock, no DB) is separated from I/O modules so it
  is unit-testable without a database.

---

## 1. Work-item state machine

### States

```
discovered → extracted → scored → { rejected | surfaced }
surfaced  → { skipped | backlog | approved }
approved  → researched → drafted → sent → closed
```

Terminal states: `rejected`, `skipped`, `closed`.
`backlog` is a holding state (re-enterable into the gate later).

### Transition table

Each transition has a **kind**:
- **deterministic** — pure rule, no human, no model. Runs automatically.
- **HITL-gated** — requires an explicit human decision recorded in the DB.
- **agent(LLM)** — driven by a model call. **Deferred** in this version
  (the interface exists; the implementation is a stub — see §6).

| # | From | To | Trigger | Kind |
|---|------|-----|---------|------|
| T1 | `discovered` | `extracted` | parse raw post into structured fields | deterministic (regex/heuristics; agent(LLM) deferred) |
| T2 | `extracted` | `scored` | compute relevance_score | deterministic (`score(item)`) |
| T3 | `scored` | `rejected` | score below threshold OR prefilter veto | deterministic |
| T4 | `scored` | `surfaced` | score at/above threshold | deterministic |
| T5 | `surfaced` | `skipped` | human dismisses | HITL-gated |
| T6 | `surfaced` | `backlog` | human defers ("later") | HITL-gated |
| T7 | `surfaced` | `approved` | human approves for application | HITL-gated |
| T8 | `backlog` | `approved` | human promotes from backlog | HITL-gated |
| T9 | `backlog` | `skipped` | human drops from backlog | HITL-gated |
| T10 | `approved` | `researched` | gather company/role context | agent(LLM) — **deferred** (stub passes through) |
| T11 | `researched` | `drafted` | generate application draft | agent(LLM) — **deferred** (stub passes through) |
| T12 | `drafted` | `sent` | human confirms + send happens | HITL-gated (network send **deferred**) |
| T13 | `sent` | `closed` | finalize / archive | deterministic |

### The `advance(item)` dispatcher

A **single** function `advance(item)` drives all transitions:

1. Reads the item's current `state`.
2. Looks up the allowed outgoing transition(s) for that state in a static
   transition map.
3. For **deterministic** transitions: evaluates the rule and moves the item.
4. For **HITL-gated** transitions: does **not** move the item on its own.
   It returns a status indicating a human decision is required, OR — when
   called with a supplied decision — validates that the decision is legal
   for the current state and applies it.
5. For **agent(LLM)** transitions: in this version the agent step is a
   deferred stub; `advance` invokes the stub which performs a pass-through
   move (or signals "deferred/no-op") so the pipeline stays testable.
6. Every successful move writes a row to `state_transitions` and updates
   `work_items.state` + `updated_at` in the **same DB transaction**.
7. Idempotency: advancing an item already in a terminal state is a no-op
   that returns a "terminal" status without error.

`advance` is the only writer of state. No other module mutates `state`.

---

## 2. SQLite schema

stdlib `sqlite3`. Datetimes stored as **ISO-8601 UTC strings** produced by
`datetime.now(timezone.utc).isoformat()` (always offset-aware). Booleans
stored as `INTEGER` (0/1). `PRAGMA foreign_keys = ON` set on every
connection.

### `work_items`

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `state` | TEXT | NOT NULL; one of the state-machine states |
| `source_channel` | TEXT | where the post came from (channel/feed name) |
| `source_link` | TEXT | link/permalink to the original post |
| `source_message_id` | TEXT | external id for dedup (nullable) |
| `raw_text` | TEXT | original post text (input to extract) |
| `extracted_json` | TEXT | JSON blob matching the Extract schema (§4); NULL until `extracted` |
| `relevance_score` | REAL | NULL until `scored` |
| `created_at` | TEXT | UTC ISO-8601, NOT NULL |
| `updated_at` | TEXT | UTC ISO-8601, NOT NULL |

Indexes / constraints:
- `idx_work_items_state` on (`state`) — pipeline scans by state.
- `idx_work_items_updated_at` on (`updated_at`).
- `uniq_work_items_source` UNIQUE on (`source_channel`, `source_message_id`)
  for dedup (partial/nullable handling: enforce only when message id present).
- `CHECK (state IN (...))` listing the legal states.

> Extracted fields (title, company, stack, salary, etc.) live inside
> `extracted_json` as a single JSON column to keep the table minimal. The
> typed JSON contract is defined in §4 and validated in pure logic, not by
> the DB.

### `state_transitions`

Append-only audit log. One row per successful `advance` move.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `item_id` | INTEGER | NOT NULL, FK → `work_items.id` |
| `from_state` | TEXT | NULL allowed for the initial insert event |
| `to_state` | TEXT | NOT NULL |
| `kind` | TEXT | `deterministic` \| `hitl` \| `agent` |
| `actor` | TEXT | `system` \| `human` \| `agent` |
| `reason` | TEXT | short note / decision rationale (nullable) |
| `created_at` | TEXT | UTC ISO-8601, NOT NULL |

Indexes:
- `idx_transitions_item` on (`item_id`).
- `idx_transitions_created_at` on (`created_at`).

---

## 3. Module layout

Pure logic (no I/O, no DB, no clock) is isolated from I/O. Pure modules are
deterministic given their inputs and are the primary unit-test targets.

```
job_hunter/
  __init__.py
  states.py          # PURE: State constants, TRANSITIONS map, transition kinds,
                     #       is_terminal(state), allowed_transitions(state)
  schema_extract.py  # PURE: ExtractResult dataclass/TypedDict + validate/parse/serialize
  extract.py         # PURE: extract(raw_text) -> ExtractResult (heuristic; LLM deferred)
  scoring.py         # PURE: prefilter(extracted) -> bool, score(item) -> float, reasons
  pipeline.py        # ORCHESTRATION: advance(item, decision=None) dispatcher;
                     #                calls pure logic, delegates persistence to store
  store.py           # I/O: sqlite3 access — connect(), init_db(), insert_item(),
                     #      get_item(), update_state(), log_transition(),
                     #      list_by_state()
  clock.py           # I/O-ish: now_utc() -> datetime (the ONLY now() call site)
  agents.py          # DEFERRED STUBS: research(item), draft(item) — pass-through no-ops

migrations/
  schema.sql         # CREATE TABLE / INDEX statements (§2)

tests/
  test_states.py        # transition map legality, terminal detection
  test_extract.py       # raw_text -> ExtractResult shape
  test_schema_extract.py# validation of the §4 contract
  test_scoring.py       # prefilter + score determinism, reasons
  test_pipeline.py      # advance() over deterministic + HITL paths (in-memory sqlite)
  test_store.py         # CRUD + transition logging + UTC strings
```

Rules:
- `states.py`, `schema_extract.py`, `extract.py`, `scoring.py` import **no**
  `sqlite3` and call **no** clock directly.
- Only `clock.py` calls `datetime.now(timezone.utc)`. Everything else takes
  time as an argument or imports `now_utc`.
- `pipeline.advance` is the sole module that combines pure decisions with
  `store` writes, inside one transaction.

---

## 4. Extract output schema (JSON)

Produced by `extract(raw_text)`, stored in `work_items.extracted_json`.
Validated by `schema_extract.py` (pure). Unknown/missing values use `null`.

```json
{
  "title":          "string",
  "company":        "string | null",
  "stack":          ["string", "..."],
  "seniority":      "string | null",
  "salary_min":     "number | null",
  "salary_max":     "number | null",
  "currency":       "string | null",
  "remote":         "boolean | null",
  "relocation":     "boolean | null",
  "location":       "string | null",
  "contact_type":   "string | null",
  "contact":        "string | null",
  "source_channel": "string",
  "source_link":    "string | null",
  "relevance_score":"number | null",
  "reasons":        ["string", "..."]
}
```

Field notes:
- `stack` and `reasons` are always present (possibly empty arrays).
- `relevance_score` is `null` after extract; populated by `score()` at T2.
- `remote` / `relocation` are tri-state via `null` (unknown vs false).
- `salary_min <= salary_max` when both present (validation warning, not a
  hard reject).
- `contact_type` is a free string in this version (e.g. `email`, `telegram`,
  `form`); no enum enforcement to stay minimal.

---

## 5. Interface signatures (no logic)

```python
# scoring.py  (PURE)
def prefilter(extracted: "ExtractResult") -> bool:
    """Cheap hard veto. True = keep, False = reject before scoring."""
    ...

def score(item: "ExtractResult") -> float:
    """Return relevance_score in [0.0, 1.0]. Pure; no I/O."""
    ...

# pipeline.py  (ORCHESTRATION)
def advance(item: "WorkItem", decision: "Decision | None" = None) -> "AdvanceResult":
    """Single dispatcher for ALL transitions.

    - Deterministic transitions run automatically.
    - HITL-gated transitions require `decision`; without it, returns a
      'needs_human' result and does not mutate state.
    - agent(LLM) transitions call deferred stubs (pass-through).
    Persists state change + appends to state_transitions atomically.
    """
    ...
```

(`WorkItem`, `ExtractResult`, `Decision`, `AdvanceResult` are simple
dataclasses / TypedDicts defined in the respective pure modules.)

---

## 6. Explicitly deferred (out of scope for this version)

These are stubbed behind stable interfaces so the core pipeline runs and is
testable, but contain **no real implementation, no secrets, no network**:

- **Userbot / Telegram read** — ingestion of real posts. This version inserts
  `work_items` from local fixtures / direct `raw_text`. No auth, no API id/hash.
- **LLM calls** — T1 (smart extract), T10 (research), T11 (draft). Replaced by
  deterministic heuristic extract and pass-through `agents.research` /
  `agents.draft` stubs. No API keys.
- **aiogram / bot UI** — the HITL surface. Human decisions are supplied to
  `advance(item, decision=...)` programmatically (e.g. in tests). No bot,
  no tokens.
- **enrich** — company/role enrichment from external sources. Deferred to the
  agent(LLM) research step.
- **draft send** — T12's actual delivery (network send) is deferred; the
  state move to `sent` is recorded but no message leaves the machine.

When deferred pieces land, they plug into the existing interfaces in
`extract.py`, `agents.py`, and the ingestion path into `store.insert_item`
— the state machine, schema, and `advance` dispatcher do not change.
