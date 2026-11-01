# Case 0003 — Postmortem: credit outage turned into deleted vacancies (2026-10-20…28)

- **Status:** Resolved; follow-ups listed below
- **Format:** blameless — what the system did and why it was possible, not who
- **Code conforms to:** `job_hunter/pipeline.py` (`_do_score`, `_defer_scoring`,
  `classify_llm_error`), `job_hunter/store.py` (`record_score_failure`),
  `job_hunter/run.py` (`scoring_alert_text`), `.github/workflows/test.yml`,
  `deploy.yml`
- **Sources:** commit `0e14afd` message, `scripts/oneoff/restore_lost_tme.py`
  docstring, operator-reported recovery figures (marked below)

## Summary

An Anthropic credit outage (2026-10-20…28) made every scoring call raise. The
pipeline caught the exception and converted it to `score = 0`; any score under
`min_persist_score` (25) is deleted together with its audit log. So during the
outage each newly harvested vacancy was scored 0 and removed, silently, for about
eight days. Nothing alerted, no test covered the path, and a deleted row leaves no
trace in the dashboard.

## Impact

- Vacancies deleted: **~650** by the estimate in the fix commit message. The
  deletion also removed their `state_transitions` audit rows, so the exact set is
  not reconstructible from the DB.
- Recovered (**operator-reported**): **594 URLs** re-collected from the container
  logs, which print each dropped URL; the recovery script re-reads `t.me` posts
  through the public reader and pushes them through the normal pipeline. **8** of
  the recovered vacancies ended up surfaced after re-scoring.
- Not established here: how many of the ~650 were not recoverable (the figures
  differ by ~56 and the difference is unexplained), and whether any non-`t.me`
  source was recovered.
- Duration: scoring was failing for the whole eight-day window, so new vacancies
  harvested in it were affected (the exact count is the ~650 above).

## Timeline

| When | Event |
|---|---|
| 2026-10-20 | Start of the credit outage (window from the fix commit); scoring calls fail. Failures become `score=0` → deletion. |
| 2026-10-20…28 | Daily harvests keep running; new vacancies are scored 0 and dropped. No alert. |
| 2026-10-28 | End of the outage window; scoring works again. |
| after 2026-10-28 | Loss discovered; how and when is **not recorded in the repo**. URLs recovered from logs. |
| 2026-10-31 17:46 | `0e14afd` — failure defers instead of deleting; attempts, `score_failed`, alert. |
| 2026-10-31 18:55 | `3e16565` — masks bot tokens / DB passwords in logs and ops messages; the same commit adds the recovery script. |
| 2026-10-31 19:04 | `5f61eaa` — CI test gate in front of deploy; two latent test/DB fixes. |
| 2026-11-01 10:06 | New alert fires for a different problem (12/46 parse failures) — first real-world use of the alert; see Case 0002. |

The start/end dates come from the fix commit and the recovery script; intermediate
hours were not reconstructed.

## Root causes and contributing factors

1. **Unknown was treated as zero.** An exception while scoring produced `score=0`,
   and the retention rule (`score < min_persist_score` → delete) was written for
   "the model judged this irrelevant". The same code path served "the model never
   judged this".
2. **Deletion was irreversible and unlogged.** `delete_item` removes the row and its
   audit trail. A policy meant to keep the database lean had no undo and no record.
3. **No alert on scoring failures.** The pipeline had no counter of failed LLM calls
   and no ops message for it.
4. **A failed day looked like a quiet day.** "0 newly surfaced" is a normal harvest
   outcome in a slow market, so the output gave no signal (contributing factor, by
   reasoning rather than measurement).
5. **No tests in CI.** Before `5f61eaa` the repository had no test workflow at all
   (`git log -- .github` shows no test workflow before that commit); deploys went out ungated,
   so a regression in the scoring path could not be caught before production.

## What changed

| Gap | Before | After |
|---|---|---|
| Unknown ≠ zero | exception → `score=0` → delete | exception → item stays `extracted`, retried next run (`_defer_scoring`) |
| Retry limit | none | `score_attempts`, `score_failed` after 3 failures; item kept, skipped by harvest, visible for manual review |
| Account-wide outage | each vacancy burned | credits/auth errors halt scoring for the run and do **not** spend the vacancy's attempts |
| Deletion rule | any score under 25 | only a score the model actually produced |
| Detection | none | one ops alert per harvest on a credits/auth error or > 20 % failed calls, with counts and error type only |
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

The incident itself cannot be re-run (it needed a drained account); the history is
in `git show 0e14afd` and `git log --oneline -- .github`.
