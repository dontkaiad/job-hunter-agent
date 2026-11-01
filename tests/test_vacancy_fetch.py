"""Fetch the real text behind link-only stubs (variant 2) + the host denylist.

No network: pages are synthetic/truncated fixtures served by an injected ``get``.
"""
import pytest

from job_hunter import llm, pipeline, research_fetch as rf, run, store, vacancy_fetch as vf
from job_hunter.pipeline import Deps
from job_hunter.states import EXTRACTED

STUB_TELETYPE = "Remote AI Job\nhttps://teletype.in/@remote_ai_jobs/AbCdEf123"
STUB_HABR = "Vacancy\nhttps://career.habr.com/vacancies/1000123456"
LONG = "We build LLM products and need an engineer to own prompts, RAG and evals. " * 8

TELETYPE_HTML = (
    "<html><head><title>x</title></head><body>About Teletype Join "
    f"<p>AI Engineer RELOCATION TO SPAIN | EXAMPLE CO</p><p>{LONG}</p>"
    "<p>APPLY</p>@author August 3, 11:45 0 views 0 reactions 0 replies 0 reposts Repost Share"
    "</body></html>"
)
REMOCATE_HTML = (
    "<html><body>remocateJobsCountriesCompaniesBlogPost a Job "
    f"<h1>Analyst at EXAMPLE</h1><p>{LONG}</p>"
    "<p>→ remocateLinkedIn Popular Countries Canada Cyprus All countries → Legal Privacy</p>"
    "</body></html>"
)


class Resp:
    def __init__(self, status=200, text="", headers=None):
        self.status_code, self.text = status, text
        self.headers = headers or {"content-type": "text/html"}


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """Treat any non-metadata/private host as safe without resolving DNS."""
    monkeypatch.setattr(rf, "_url_is_safe", lambda u: "169.254" not in u and "127.0.0.1" not in u)


def _get_returning(resp):
    calls = []

    def get(url, **kw):
        calls.append(url)
        return resp if not callable(resp) else resp(url)
    get.calls = calls
    return get


# --- denylist ---------------------------------------------------------------

@pytest.mark.parametrize("host", [
    "habr.com", "career.habr.com", "www.habr.com", "getmatch.ru", "WWW.GetMatch.ru",
    "linkedin.com", "www.linkedin.com", "ru.linkedin.com",
])
def test_denylisted_hosts(host):
    assert vf.host_denied(host, vf.DEFAULT_DENYLIST)


@pytest.mark.parametrize("host", ["teletype.in", "remocate.app", "notlinkedin.com", "habr.com.evil.io", None, ""])
def test_not_denylisted_hosts(host):
    assert not vf.host_denied(host, vf.DEFAULT_DENYLIST)


def test_denylisted_link_makes_no_request():
    get = _get_returning(Resp(text=TELETYPE_HTML))
    res = vf.fetch_vacancy_text(STUB_HABR, get=get)
    assert res.status == vf.DENYLISTED and res.host == "career.habr.com"
    assert get.calls == []


def test_denylist_is_configurable():
    get = _get_returning(Resp(text=TELETYPE_HTML))
    res = vf.fetch_vacancy_text(STUB_TELETYPE, denylist=("teletype.in",), get=get)
    assert res.status == vf.DENYLISTED and get.calls == []


def test_redirect_onto_denylisted_host_is_dropped():
    def r(url):
        if "teletype" in url:
            return Resp(302, headers={"location": "https://www.linkedin.com/jobs/1"})
        return Resp(text=TELETYPE_HTML)
    res = vf.fetch_vacancy_text(STUB_TELETYPE, get=_get_returning(r))
    assert res.status == vf.DENYLISTED and res.text == ""


# --- fetching + cleanup -----------------------------------------------------

def test_teletype_page_is_fetched_and_chrome_removed():
    res = vf.fetch_vacancy_text(STUB_TELETYPE, get=_get_returning(Resp(text=TELETYPE_HTML)))
    assert res.status == vf.OK and res.host == "teletype.in"
    assert res.text.startswith("AI Engineer RELOCATION TO SPAIN")
    assert "About Teletype" not in res.text and "views" not in res.text


def test_remocate_page_is_fetched_and_chrome_removed():
    res = vf.fetch_vacancy_text(
        "Analyst at EXAMPLE: https://www.remocate.app/jobs/analyst-example",
        get=_get_returning(Resp(text=REMOCATE_HTML)),
    )
    assert res.status == vf.OK
    assert res.text.startswith("Analyst at EXAMPLE") and "Popular Countries" not in res.text


def test_text_is_capped():
    big = TELETYPE_HTML.replace(LONG, LONG * 40)
    res = vf.fetch_vacancy_text(STUB_TELETYPE, get=_get_returning(Resp(text=big)), max_chars=1000)
    assert res.status == vf.OK and len(res.text) <= 1000


def test_only_telegram_link_is_no_url():
    get = _get_returning(Resp(text=TELETYPE_HTML))
    res = vf.fetch_vacancy_text("see https://t.me/somechannel/123", "https://t.me/somechannel/123", get=get)
    assert res.status == vf.NO_URL and get.calls == []


@pytest.mark.parametrize("resp", [
    Resp(404, "nope"), Resp(500, "err"), Resp(200, "<html><body>tiny</body></html>"),
    Resp(200, "%PDF", {"content-type": "application/pdf"}),
])
def test_unusable_responses_fail_cleanly(resp):
    assert vf.fetch_vacancy_text(STUB_TELETYPE, get=_get_returning(resp)).status == vf.FAILED


def test_redirect_to_metadata_ip_is_blocked():
    def r(url):
        return Resp(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
    res = vf.fetch_vacancy_text(STUB_TELETYPE, get=_get_returning(r))
    assert res.status == vf.FAILED


def test_network_exception_never_raises():
    def boom(url, **kw):
        raise OSError("down")
    assert vf.fetch_vacancy_text(STUB_TELETYPE, get=boom).status == vf.FAILED


# --- pipeline integration ---------------------------------------------------

class ToolLLM:
    def __init__(self, *results):
        self.results, self.users = list(results), []

    def complete(self, *a, **k):  # pragma: no cover
        raise AssertionError("text path used")

    def complete_tool(self, system, user, tool, **kw):
        self.users.append(user)
        return self.results.pop(0)


SCORE = lambda n: {"relevance_score": n, "insufficient_content": False, "reasoning": f"{n}/100"}


def _setup(conn, fake_fx, client, fetch_fn=None, text=STUB_TELETYPE):
    deps = Deps(llm_client=client, fx=fake_fx, use_llm_extract=False, min_persist_score=25,
                fetch_enabled=True, fetch_fn=fetch_fn)
    item_id = store.insert_item(conn, raw_text=text, source_channel="@c", source_message_id="1")
    pipeline.advance_by_id(conn, item_id, deps=deps)  # extract
    return deps, item_id


def _ok_fetch(calls):
    def fn(raw, link, denylist=()):
        calls.append(raw)
        return vf.FetchResult(vf.OK, url="https://teletype.in/x", host="teletype.in", text=LONG)
    return fn


def test_stub_is_fetched_then_scored_with_page_text(conn, fake_fx):
    client, calls = ToolLLM(SCORE(70)), []
    deps, item_id = _setup(conn, fake_fx, client, _ok_fetch(calls))

    results = pipeline.run_to_gate(conn, item_id, deps=deps)

    it = store.get_item(conn, item_id)
    assert len(calls) == 1
    assert "FETCHED PAGE TEXT" in client.users[0] and LONG[:40] in client.users[0]
    assert it.fetched_text == LONG and it.fetch_status == "ok"
    assert it.defer_reason is None and it.score_attempts == 0
    assert any(r.to_state == "surfaced" for r in results)
    assert deps.score_stats.fetched == 1 and deps.score_stats.link_only == 0


def test_fetched_text_is_reused_not_refetched(conn, fake_fx):
    client, calls = ToolLLM({"relevance_score": None, "insufficient_content": True, "reasoning": "x"}, SCORE(60)), []
    deps, item_id = _setup(conn, fake_fx, client, _ok_fetch(calls))
    pipeline.advance_by_id(conn, item_id, deps=deps)   # fetch ok, LLM says insufficient
    assert store.get_item(conn, item_id).defer_reason == "insufficient"
    pipeline.advance_by_id(conn, item_id, deps=deps)   # retry scoring
    assert len(calls) == 1, "page must not be fetched twice"


def test_real_low_score_after_fetch_is_deleted(conn, fake_fx):
    client = ToolLLM(SCORE(12))
    deps, item_id = _setup(conn, fake_fx, client, _ok_fetch([]))
    pipeline.run_to_gate(conn, item_id, deps=deps)
    assert store.get_item(conn, item_id) is None


def test_denylisted_stub_stays_link_only_and_never_requests(conn, fake_fx, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("network used for a denylisted host")
    monkeypatch.setattr(rf, "_default_get", forbidden)
    client = ToolLLM()
    deps, item_id = _setup(conn, fake_fx, client, text=STUB_HABR)

    for _ in range(2):  # two harvests
        res = pipeline.advance_by_id(conn, item_id, deps=deps)

    it = store.get_item(conn, item_id)
    assert res.status == "noop" and it.state == EXTRACTED
    assert it.defer_reason == "link_only" and it.fetch_status == "denylisted"
    assert it.fetch_attempts == 0 and it.score_attempts == 0 and it.fetched_text is None
    assert client.users == []
    assert deps.score_stats.denylisted == 2 and deps.score_stats.fetched == 0


def test_failed_fetch_is_retried_max_times_without_spending_score_attempts(conn, fake_fx):
    calls = []

    def failing(raw, link, denylist=()):
        calls.append(1)
        return vf.FetchResult(vf.FAILED, url="https://teletype.in/x", host="teletype.in")
    client = ToolLLM()
    deps, item_id = _setup(conn, fake_fx, client, failing)

    for _ in range(5):
        pipeline.advance_by_id(conn, item_id, deps=deps)

    it = store.get_item(conn, item_id)
    assert len(calls) == 3 == it.fetch_attempts
    assert it.defer_reason == "link_only" and it.fetch_status == "failed"
    assert it.score_attempts == 0 and not it.score_failed and client.users == []


def test_normal_vacancy_never_triggers_a_fetch(conn, fake_fx):
    calls = []
    client = ToolLLM(SCORE(70))
    text = ("Remote Senior Python engineer building LLM RAG agents with Claude API and "
            "FastAPI. Salary 250000-350000 RUB. Contact @hr_acme https://example.com/job")
    deps, item_id = _setup(conn, fake_fx, client, _ok_fetch(calls), text=text)
    pipeline.run_to_gate(conn, item_id, deps=deps)
    assert calls == [] and store.get_item(conn, item_id).fetch_status is None


def test_fetch_disabled_keeps_old_link_only_behaviour(conn, fake_fx):
    client = ToolLLM()
    deps, item_id = _setup(conn, fake_fx, client, _ok_fetch([]))
    deps.fetch_enabled = False
    pipeline.advance_by_id(conn, item_id, deps=deps)
    assert store.get_item(conn, item_id).defer_reason == "link_only" and client.users == []


# --- ops lines --------------------------------------------------------------

def test_fetch_counts_never_make_the_alert_red_and_are_reported():
    s = pipeline.ScoreRunStats(attempted=10, fetched=5, fetch_failed=2, denylisted=3, link_only=4)
    assert run.scoring_alert_text(s) is None
    info = run.parked_info_text(s)
    assert "fetch: ok 5, failed 2, denylisted 3" in info and "link_only 4" in info


def test_red_alert_includes_fetch_line():
    s = pipeline.ScoreRunStats(attempted=10, failed=5, fetched=1)
    s.kinds["parse"] = 5
    assert "fetch: ok 1, failed 0, denylisted 0" in run.scoring_alert_text(s)
