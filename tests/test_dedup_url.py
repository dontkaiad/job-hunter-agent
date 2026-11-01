"""Dedup by the vacancy's main link (not only the post's own source_link).

Case: one teletype page posted in three channels -> three work items with different
Telegram permalinks, one vacancy.
"""
import pytest

from job_hunter import pipeline, store, vacancy_fetch as vf
from job_hunter.pipeline import Deps
from job_hunter.vacancy_url import canonical_url, primary_url_key

LONG = "We build LLM products and need an engineer to own prompts, RAG and evals. " * 8
BASE = "https://teletype.in/@remote_ai_jobs/AbCdEf123"


# --- canonical_url ----------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    (BASE, BASE + "/"),
    (BASE, "https://TeleType.IN/@remote_ai_jobs/AbCdEf123"),
    (BASE, "http://www.teletype.in/@remote_ai_jobs/AbCdEf123"),
    (BASE, BASE + "?utm_source=tg&utm_medium=post&utm_campaign=x"),
    (BASE, BASE + "#comments"),
    (BASE, BASE + "?fbclid=abc&gclid=def&yclid=1&ref=tg&ref_src=x&mc_cid=1&mc_eid=2&igshid=3"),
    (BASE, "https://teletype.in:443/@remote_ai_jobs/AbCdEf123"),
    ("https://x.io/job?id=7&lang=en", "https://x.io/job?lang=en&id=7&utm_source=a"),
])
def test_same_vacancy_same_key(a, b):
    assert canonical_url(a) == canonical_url(b) and canonical_url(a)


@pytest.mark.parametrize("a,b", [
    (BASE, "https://teletype.in/@remote_ai_jobs/abcdef123"),      # path is case-sensitive
    ("https://x.io/job?id=1", "https://x.io/job?id=2"),            # meaningful query differs
    (BASE, "https://teletype.in/@other/AbCdEf123"),
    ("https://a.example/job/1", "https://b.example/job/1"),
])
def test_different_vacancies_different_key(a, b):
    assert canonical_url(a) != canonical_url(b)


@pytest.mark.parametrize("url", [
    "https://weworkremotely.com", "https://weworkremotely.com/", "https://x.io/?utm_source=a",
    "ftp://x.io/a", "not a url", "", None,
])
def test_landing_pages_and_junk_have_no_key(url):
    assert canonical_url(url) is None


def test_primary_url_key_ignores_telegram_links():
    assert primary_url_key(f"x {BASE}", "https://t.me/chan/5") == canonical_url(BASE)
    assert primary_url_key("no link here", "https://t.me/chan/5") is None


# --- pipeline ---------------------------------------------------------------

class ToolLLM:
    def __init__(self, *results):
        self.results, self.calls = list(results), 0

    def complete(self, *a, **k):  # pragma: no cover
        raise AssertionError("text path used")

    def complete_tool(self, *a, **k):
        self.calls += 1
        return self.results.pop(0)


def SCORE(n):
    return {"relevance_score": n, "insufficient_content": False, "reasoning": f"{n}/100"}


def _deps(client, fake_fx, fetch_fn):
    return Deps(llm_client=client, fx=fake_fx, use_llm_extract=False, min_persist_score=25,
                fetch_enabled=True, fetch_fn=fetch_fn)


def _post(conn, deps, text, channel, mid):
    item_id = store.insert_item(conn, raw_text=text, source_channel=channel,
                                source_link=f"https://t.me/{channel.strip('@')}/{mid}",
                                source_message_id=mid)
    pipeline.advance_by_id(conn, item_id, deps=deps)  # extract
    return item_id


def _fetch_counter(final_url=None):
    """Fake fetcher: lands on the post's own link unless `final_url` forces a redirect."""
    calls = []

    def fn(raw, link, denylist=()):
        calls.append(raw)
        url = final_url or vf.pick_url(raw, link)
        return vf.FetchResult(vf.OK, url=url, host="x", text=LONG)
    fn.calls = calls
    return fn


def test_one_teletype_page_in_three_channels_keeps_only_the_first(conn, fake_fx):
    fetch, llm = _fetch_counter(), ToolLLM(SCORE(40))
    deps = _deps(llm, fake_fx, fetch)
    a = _post(conn, deps, f"Remote AI Job\n{BASE}", "@Relocats", "1")
    b = _post(conn, deps, f"Relocation job\nhttps://TeleType.in/@remote_ai_jobs/AbCdEf123/?utm_source=tg", "@Remoteit", "2")
    c = _post(conn, deps, f"Job link: http://www.teletype.in/@remote_ai_jobs/AbCdEf123#top", "@jobfeeds", "3")

    for i in (a, b, c):  # one harvest, ascending ids
        pipeline.run_to_gate(conn, i, deps=deps)

    assert store.get_item(conn, a) is not None, "the first copy is canonical"
    assert store.get_item(conn, b) is None and store.get_item(conn, c) is None
    assert len(fetch.calls) == 1 and llm.calls == 1, "duplicates cost no fetch and no LLM call"
    assert deps.score_stats.duplicates == 2 and deps.score_stats.failed == 0


def test_different_path_case_is_not_a_duplicate(conn, fake_fx):
    fetch, llm = _fetch_counter(), ToolLLM(SCORE(40), SCORE(40))
    deps = _deps(llm, fake_fx, fetch)
    a = _post(conn, deps, f"Job\n{BASE}", "@a", "1")
    b = _post(conn, deps, "Job\nhttps://teletype.in/@remote_ai_jobs/abcdef123", "@b", "2")
    for i in (a, b):
        pipeline.run_to_gate(conn, i, deps=deps)
    assert store.get_item(conn, a) and store.get_item(conn, b)
    assert llm.calls == 2


def test_different_meaningful_query_is_not_a_duplicate(conn, fake_fx):
    fetch, llm = _fetch_counter(), ToolLLM(SCORE(40), SCORE(40))
    deps = _deps(llm, fake_fx, fetch)
    a = _post(conn, deps, "Python LLM engineer wanted, remote work\nhttps://x.io/job?id=1", "@a", "1")
    b = _post(conn, deps, "Python LLM engineer wanted, remote work\nhttps://x.io/job?id=2", "@b", "2")
    for i in (a, b):
        pipeline.run_to_gate(conn, i, deps=deps)
    assert store.get_item(conn, a) and store.get_item(conn, b) and llm.calls == 2


def test_site_root_links_never_merge_posts(conn, fake_fx):
    fetch, llm = _fetch_counter(), ToolLLM(SCORE(40), SCORE(40))
    deps = _deps(llm, fake_fx, fetch)
    a = _post(conn, deps, "Great job board https://weworkremotely.com/", "@a", "1")
    b = _post(conn, deps, "Another post https://weworkremotely.com", "@b", "2")
    for i in (a, b):
        pipeline.run_to_gate(conn, i, deps=deps)
    assert store.get_item(conn, a) and store.get_item(conn, b)
    assert deps.score_stats.duplicates == 0


def test_repost_of_an_already_sent_vacancy_is_dropped(conn, fake_fx):
    fetch, llm = _fetch_counter(), ToolLLM()
    deps = _deps(llm, fake_fx, fetch)
    old = store.insert_item(conn, raw_text=f"Old post\n{BASE}", source_channel="@old",
                            source_message_id="1", state="sent")
    store.set_url_keys(conn, old, primary=canonical_url(BASE))  # what the backfill does
    new = _post(conn, deps, f"Again\n{BASE}?utm_source=tg", "@new", "2")
    pipeline.run_to_gate(conn, new, deps=deps)
    assert store.get_item(conn, new) is None and store.get_item(conn, old).state == "sent"
    assert fetch.calls == [] and llm.calls == 0


def test_two_short_links_resolving_to_one_page_are_merged_after_fetch(conn, fake_fx):
    fetch, llm = _fetch_counter(final_url=BASE), ToolLLM(SCORE(40))
    deps = _deps(llm, fake_fx, fetch)
    a = _post(conn, deps, "Job\nhttps://short.example/aaa", "@a", "1")
    b = _post(conn, deps, "Job\nhttps://short.example/bbb", "@b", "2")
    for i in (a, b):
        pipeline.run_to_gate(conn, i, deps=deps)
    assert store.get_item(conn, a) and store.get_item(conn, b) is None
    assert store.get_item(conn, a).final_url_key == canonical_url(BASE)
    assert len(fetch.calls) == 2 and llm.calls == 1, "second one is caught by the final URL"
    assert deps.score_stats.duplicates == 1


def test_older_id_is_always_the_canonical_one(conn, fake_fx):
    a = store.insert_item(conn, raw_text=f"x\n{BASE}", source_channel="@a", source_message_id="1")
    b = store.insert_item(conn, raw_text=f"x\n{BASE}", source_channel="@b", source_message_id="2")
    key = canonical_url(BASE)
    store.set_url_keys(conn, a, primary=key)
    store.set_url_keys(conn, b, primary=key)
    assert store.find_duplicate_by_url_keys(conn, [key], b) == a
    assert store.find_duplicate_by_url_keys(conn, [key], a) is None  # never mutual


def test_schema_adds_columns_and_indexes_to_an_old_table(pg_dsn):
    c = store.connect(pg_dsn)
    store.init_db(c)
    c.execute("DROP INDEX IF EXISTS idx_work_items_primary_url")
    c.execute("DROP INDEX IF EXISTS idx_work_items_final_url")
    c.execute("ALTER TABLE work_items DROP COLUMN primary_url_key")
    c.execute("ALTER TABLE work_items DROP COLUMN final_url_key")
    c.commit()
    store.init_db(c)  # must re-add columns BEFORE indexing them
    idx = {r["indexname"] for r in c.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename='work_items'").fetchall()}
    assert {"idx_work_items_primary_url", "idx_work_items_final_url"} <= idx
    c.close()


def test_backfill_plan_finds_groups():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "bf", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "oneoff" / "backfill_url_keys.py")
    bf = importlib.util.module_from_spec(spec); spec.loader.exec_module(bf)
    rows = [
        {"id": 1, "state": "sent", "raw_text": f"a {BASE}", "source_link": None, "primary_url_key": None},
        {"id": 2, "state": "extracted", "raw_text": f"b {BASE}/?utm_source=x", "source_link": None, "primary_url_key": None},
        {"id": 3, "state": "extracted", "raw_text": "no link", "source_link": "https://t.me/c/1", "primary_url_key": None},
        {"id": 4, "state": "scored", "raw_text": "z", "source_link": None, "primary_url_key": canonical_url(BASE)},
    ]
    new_keys, groups = bf.plan(rows)
    assert set(new_keys) == {1, 2} and 3 not in new_keys
    assert [m[0] for m in groups[canonical_url(BASE)]] == [1, 2, 4]


def test_ops_info_mentions_duplicates():
    from job_hunter import run
    s = pipeline.ScoreRunStats(attempted=3, duplicates=2)
    assert run.scoring_alert_text(s) is None
    assert "duplicates dropped (same vacancy URL): 2" in run.parked_info_text(s)
