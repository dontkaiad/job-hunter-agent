# Case 0003 — Postmortem: credit outage turned into deleted vacancies (2026-10-20…28)

- **Status:** Resolved; follow-ups listed below
- **Format:** blameless — what the system did and why it was possible, not who
- **Code conforms to:** `job_hunter/pipeline.py` (`_do_score`, `_defer_scoring`,
  `classify_llm_error`), `job_hunter/store.py` (`record_score_failure`),
  `job_hunter/run.py` (`scoring_alert_text`), `.github/workflows/test.yml`,
  `deploy.yml`
- **Sources:** commit `0e14afd` message, `scripts/oneoff/restore_lost_tme.py`
  docstring, log extracts on the server (counts re-checked 2026-11-01), the phase-5
  recovery report (marked, partly re-checked against the DB), and operator-reported
  facts (marked below)

## Summary

An Anthropic credit outage (2026-10-20…28) made every scoring call raise. The
pipeline caught the exception and converted it to `score = 0`; any score under
`min_persist_score` (25) is deleted together with its audit log. So during the
outage each newly harvested vacancy was scored 0 and removed, silently, for nine
daily harvests. **Nothing in the system signalled it: the operator noticed on
2026-10-31 that the dashboard had shown no new vacancies for about a week.** That
absence of any automatic signal is the main finding of this postmortem.

## Impact

Counts below were recomputed on 2026-11-01 from the log extracts kept on the server
(`jh-lost-2026-10-20_28.txt`, saved 2026-10-31 13:08 UTC, and the two URL lists
derived from it).

- **652 drop events**, every one `score=0.0 < 25`, over nine daily harvests
  (07:02 UTC): 80, 70, 87, 90, 87, 40, 42, 76, 80 per day for 10-20 … 10-28. The
  "~650" in the fix commit (`0e14afd`) was the operator's estimate made before the
  logs were counted; the count confirms it.
- **594 unique URLs.** 652 events vs 594 URLs is not a discrepancy: the same source
  URL can produce several work items (e.g. a digest post split into pieces), so
  58 events repeat a URL already counted. By source: 421 `t.me`, 173 other (110
  jobicy.com, 35 weworkremotely.com, 28 career.habr.com, the rest long-tail).
- **593 URLs** are in `jh-lost-urls-failed-only.txt` — the list the operator
  describes as the URLs whose scoring errored (`haiku=err`). The log extract itself
  contains only the `dropped … score=0.0` lines, no `haiku=err` lines, so the
  selection criterion for 593 vs 594 cannot be re-derived from it; the one-URL
  difference is unexplained and immaterial.
- The deletion also removed the items' `state_transitions` rows, so the DB holds no
  trace of them; the logs were the only record.
- **Recovery.** Two paths, both re-scoring through the normal pipeline (figures are
  **per the phase-5 recovery report**; what could be re-checked against the DB on
  2026-11-01 is listed below the tables). The units differ between columns — URLs
  versus vacancies — so the figures are split by unit:

  | Source | URLs | already in DB (URLs) | dead (URLs) | live URLs → vacancies |
  |---|---|---|---|---|
  | `t.me` (`restore_lost_tme.py`) | 421 | 0 | 22 | 421 − 22 = 399 → **435** (digest posts split into several vacancies) |
  | web (standard `add_by_url`) | 173 | 5 | 6 | 173 − 5 − 6 = 162 → 162 (one vacancy per URL) |

  | Source | Vacancies | < 25 again, deleted | returned | returned: rejected / surfaced / deferred |
  |---|---|---|---|---|
  | `t.me` | 435 | 355 | 80 | 63 / 6 / 11 |
  | web | 162 | 105 | 57 | 51 / 2 / 4 |

  Total surfaced: **8**. LLM calls for the whole recovery: Haiku 795, Sonnet 14.

  **Checked against the DB** (rows whose `source_link` is one of the 594 lost URLs and
  that were created after the outage, 2026-10-29…31): `t.me` — exactly **80** restored
  rows, 0 older rows; web — **60** restored rows and no pre-outage rows; **6 + 2 = 8**
  of them have a `surfaced` transition, matching the report's total. Their states
  today have moved on as the operator acted on them (e.g. `rejected` 66 + 57, a few
  `sent` / `drafted` / `declined`, 8 + 1 still `extracted`), so the report's
  rejected / deferred split cannot be reproduced from current states.

  **Not checkable from the DB:** the dead URL counts and the "< 25 again, deleted"
  counts (a deleted row leaves nothing behind; 341 `t.me` and 113 web URLs have no row
  today, which includes the dead ones) and the call counts (no per-call record).
  The `t.me` line only adds up once the units are separated, as above: 435 vacancies =
  355 deleted + 80 returned, from 399 live URLs (421 − 22 dead); the vacancy count comes
  from the phase-5 report, which states 435 vacancies from 421 URLs. The web line adds
  up in both units (5 + 6 + 105 + 57 = 173); the DB shows 60 restored web rows against
  the report's 57 returned, a difference of 3 that is not explained (the report's 5
  "already in DB" may account for part of it).

## Timeline

| When | Event |
|---|---|
| 2026-10-20 07:02 UTC | First drop in the extract. Start of the credit outage (window per the fix commit); scoring calls fail → `score=0` → deletion. |
| 2026-10-20…28 | Nine daily harvests drop 40–90 vacancies each. No alert, no error visible to the operator. |
| 2026-10-28 07:02 UTC | Last drop in the extract; end of the outage window. |
| 2026-10-31 (before 13:08 UTC) | **Operator notices zero new vacancies on the dashboard for about a week.** No automatic signal existed. Dropped URLs are extracted from the container logs (saved 13:08 UTC). |
| 2026-10-31 17:46 MSK | `0e14afd` — failure defers instead of deleting; attempts, `score_failed`, alert. |
| 2026-10-31 18:55 MSK | `3e16565` — masks bot tokens / DB passwords in logs and ops messages; the same commit adds the recovery script. |
| 2026-10-31 19:04 MSK | `5f61eaa` — CI test gate in front of deploy; two latent test/DB fixes. |
| 2026-11-01 10:06 | New alert fires for a different problem (12/46 parse failures) — its first real-world use; see Case 0002. |

Times are as recorded (server logs in UTC, commits in MSK); intermediate hours were
not reconstructed.

## What went wrong

1. **The system could not tell us it was failing — a human noticed after about a
   week.** No counter of failed LLM calls, no ops message, no heartbeat on
   "candidates scored vs dropped". The failure was visible only as the *absence* of
   new vacancies, which also looks like a quiet market (zero new vacancies is a normal
   harvest outcome), so even a person watching the dashboard needs days to be sure.
   This was the dominant factor: every other cause below only mattered because
   nothing detected it.
2. **Unknown was treated as zero.** An exception while scoring produced `score=0`,
   and the retention rule (`score < min_persist_score` → delete) was written for
   "the model judged this irrelevant". The same code path served "the model never
   judged this".
3. **Deletion was irreversible and unlogged.** `delete_item` removes the row and its
   audit trail. A policy meant to keep the database lean had no undo and no record;
   recovery depended on container logs happening to print each URL.
4. **No tests in CI.** Before `5f61eaa` the repository had no test workflow
   (`git log -- .github` shows none before that commit); deploys went out ungated,
   so a regression in the scoring path could not be caught before production.

## What changed

| Gap | Before | After |
|---|---|---|
| Unknown ≠ zero | exception → `score=0` → delete | exception → item stays `extracted`, retried next run (`_defer_scoring`) |
| Retry limit | none | `score_attempts`, `score_failed` after 3 failures; item kept, skipped by harvest, visible for manual review |
| Account-wide outage | each vacancy burned | credits/auth errors halt scoring for the run and do **not** spend the vacancy's attempts |
| Deletion rule | any score under 25 | only a score the model actually produced |
| Detection | none — found by a person after ~a week | one ops alert per harvest on a credits/auth error or > 20 % failed calls, with counts and error type only |
| Regression gate | none | `test.yml` runs the suite; `deploy.yml` needs it green |
| Regression tests | none | `test_credit_error_defers_and_halts_run_scoring`, `test_parse_error_defers_and_counts_attempt`, `test_three_failures_mark_score_failed_but_keep_item`, `test_normal_low_score_is_still_deleted` (a real low score is still dropped) |

## What we did not fix (follow-ups)

- Deletion is still permanent for genuine low scores, with the audit rows. Whether
  a soft-delete / short retention window is worth it is open.
- There is no heartbeat for "harvest produced N candidates vs usual"; a silent
  upstream failure outside scoring (a source returning nothing) would still look
  like a quiet day.
- The alert has had one real-world firing since (Case 0002); its thresholds
  (> 20 %) are unvalidated beyond that.

## Reproduce

The fix is encoded in regression tests (needs the test Postgres described in
`tests/conftest.py`):

```bash
python -m pytest tests/test_pipeline.py -k "credit_error or parse_error or three_failures or normal_low_score" -q
```

The impact counts can be re-checked on the server that holds the log extracts:

```bash
grep -c dropped ~/jh-lost-2026-10-20_28.txt                           # 652 events
grep -o "url='[^']*'" ~/jh-lost-2026-10-20_28.txt | sort -u | wc -l   # 594 unique URLs
cut -c1-10 ~/jh-lost-2026-10-20_28.txt | sort | uniq -c               # per day
```

The incident itself cannot be re-run (it needed a drained account); the history is
in `git show 0e14afd` and `git log --oneline -- .github`.
