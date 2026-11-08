# Case 0004 — Recovering link-only stubs by fetching the linked page

- **Status:** Accepted (shipped in `a020eb8`, 2026-11-01); first live results below
- **Date:** 2026-11-01
- **Code conforms to:** `job_hunter/vacancy_fetch.py`, `job_hunter/research_fetch.py`
  (shared SSRF-guarded fetcher), `job_hunter/pipeline.py` (`_maybe_fetch`),
  `evals/link_fetch_eval.py`, `scripts/oneoff/fetch_link_only_stubs.py`
- **Baseline:** [`evals/results/link_fetch_2026-11-01.json`](../../evals/results/link_fetch_2026-11-01.json)

## Summary

Many Telegram posts are just a label and a link to the real vacancy (a teletype page,
a job-board card). With no text, the scorer either refuses or guesses from the URL
slug. Such posts are now parked (`link_only`), the page behind the link is fetched
once, the page text is stored (`fetched_text`) and passed to the scorer as a separate
block. Hosts whose terms forbid storing content (and one that is unreachable from the
server's region) are on a config denylist and are never requested.

## Methodology

- **What was measured:** for the stubs currently waiting in `extracted`, (1) did the
  fetch return usable text, (2) what would the scorer give with that text.
- **How:** the production fetcher (SSRF guard, redirect re-validation, 8 s timeout,
  1 redirect, 6000-char cap, chrome-stripping for two hosts) then the production
  scorer on Haiku, `temperature=0`, forced tool. Dry-run: **nothing written to the
  DB**. Candidates: state `extracted`, not `score_failed`, parked or link-only by the
  40-character gate.
- **Sample:** **n = 8** candidates, 2026-11-01 after deploy. Run twice: the first as
  the dry-run script (`scripts/oneoff/fetch_link_only_stubs.py`), the second as
  `evals/link_fetch_eval.py run`, which produced the committed baseline. The two
  agreed on every count and every host.
- **Denylist** (`FETCH_DENYLIST`): `habr.com`, `career.habr.com`, `getmatch.ru`,
  `linkedin.com` (subdomains included). A denylisted stub makes **no request**, stays
  `link_only`, `fetch_status = denylisted`. None of the 8 candidates were on it, so
  that branch is covered by unit tests, not by this live run.

## Results

Fetch: **7 of 8 returned usable text** (`fetch_status`: ok 7, failed 1), 1 failed (cause not diagnosed — e.g. a JS-only
or bot-protected page).

| Host | Fetch | Score with the page text |
|---|---|---|
| teletype.in (3 work items) | ok, 2671 chars each | 18, 18, 18 → below 25 |
| yoailabs.careers-page.com | ok, 2170 chars | 28 |
| get.it | ok, 5769 chars | 28 |
| opentrain.ai | ok, 6000 chars (cap) | 18 → below 25 |
| seoulstart.com | ok, 1397 chars | `insufficient_content` → stays parked |
| euremotejobs.com | failed | — (retried up to 3 harvests, then manual) |

Score buckets over the 7 fetched: **4 below 25** (would be deleted by the normal
rule), **2 in 25–59**, **0 at ≥ 60 — nothing would surface**, 1 insufficient.

What this does and does not say:
- Fetching worked on teletype.in (3 of 8 candidates) and on four long-tail hosts. Across
  all stub-like posts in the DB, teletype.in and remocate.app are the two biggest hosts
  (67 of 112); remocate.app was not among this run's 8, so its path is covered by unit
  tests and a manual check of one page, not by this live result.
- Three work items are **the same teletype page**: the script now hashes fetched text,
  and the 7 fetched pages are only **5 distinct pages**, with exactly one page seen more
  than once (3 items, identical text and score). One vacancy cross-posted in three
  channels, not deduplicated because dedup keys on the post's permalink, not the
  vacancy link. A follow-up (dedup by the vacancy URL) is in progress.
- 0 surfaced out of 7: for this batch, recovering the text mostly confirmed the
  stubs were not relevant. That is a fact about this batch, not a rate.

## Limitations

- **n = 8, one day.** Host-level success rates (7/8 overall) have wide uncertainty;
  one host appears once.
- **Fetch success ≠ the right text.** A 200 with 1.4 kB of text was scored as
  `insufficient_content`; whether it really was a thin page was not checked by hand.
- **Relevance was not measured.** No golden set; the scores are from the same model
  and prompt as production (without the Sonnet corridor judge in this dry-run), so
  "would be deleted" is a prediction of the pipeline's own rule, not a verdict that
  the vacancies are irrelevant. Deleting on 18 is the existing, unchanged semantics.
- Stored text is third-party page content. The denylist covers the known
  no-storage hosts; other sites' terms were not reviewed individually.

**Next step:** score-accuracy eval against the operator's real approve / skip
decisions, to check whether low scores on recovered pages agree with a human
(Case 0001).

## Reproduce

Offline (synthetic pages: chrome stripping, size cap, denylist makes no request,
Telegram-only link, unusable pages; runs in CI):

```bash
python -m evals offline
```

Live dry-run (network fetches + one Haiku call per fetched page, **≈ $0.05** for 8:
estimate from [COST.md](../../COST.md) rates; read-only DB, nothing written):

```bash
python -m evals.link_fetch_eval run --max-usd 0.3 --save my_run.json
```
