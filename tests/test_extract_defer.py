"""An API/infra failure in extract DEFERS the vacancy; it never falls back to heuristics.

The same antipattern as score=0 in October: a failure silently lowers the quality of the
input that scoring - and the below-the-cut deletion - then relies on.
"""
import pytest

from job_hunter import pipeline, run, store
from job_hunter.pipeline import Deps

POST = ("Remote Senior Python engineer building LLM RAG agents with Claude API and "
        "FastAPI. Salary 250000-350000 RUB. Contact @hr_acme")


class Boom(Exception):
    pass


def _credit_error():
    e = Exception("Your credit balance is too low to access the Anthropic API")
    e.status_code = 400
    return e


def _rate_limit():
    class RateLimitError(Exception):
        status_code = 429
    return RateLimitError("slow down")


class ExtractLLM:
    """Fake client whose extract call raises (or returns scripted JSON)."""

    def __init__(self, *plan):
        self.plan, self.calls = list(plan), 0

    def complete(self, system, user, **kw):
        self.calls += 1
        step = self.plan.pop(0) if self.plan else _credit_error()
        if isinstance(step, BaseException):
            raise step
        return step

    def complete_tool(self, *a, **k):
        raise AssertionError("scoring must not run for a deferred extract")


GOOD_JSON = ('{"title": "LLM Engineer", "company": "Acme", "stack": ["python"], "seniority": "senior", '
             '"remote": true, "relocation": false, "location": null, "salary_min": null, "salary_max": null, '
             '"currency": null, "contact": "@hr_acme", "contact_type": "dm", "benefits": [], "reasons": []}')


def _deps(client, fx, **kw):
    return Deps(llm_client=client, fx=fx, use_llm_extract=True, min_persist_score=25,
                score_stats=pipeline.ScoreRunStats(halt_on_fatal=True), **kw)


def _item(conn, mid="1", text=POST):
    return store.insert_item(conn, raw_text=text, source_channel="@c", source_message_id=mid)


# --- no heuristic fallback ---------------------------------------------------

@pytest.mark.parametrize("err,kind", [(_credit_error(), "credits/auth"), (_rate_limit(), "rate-limit/5xx/timeout"),
                                       (ValueError("no JSON"), "parse")])
def test_api_failure_defers_instead_of_heuristics(conn, fake_fx, err, kind):
    client = ExtractLLM(err)
    deps = _deps(client, fake_fx)
    iid = _item(conn)
    res = pipeline.advance_by_id(conn, iid, deps=deps)
    it = store.get_item(conn, iid)
    assert res.status == "noop" and "extract deferred" in res.reason
    assert it.state == "discovered" and it.extracted_json is None and it.extract_mode is None
    assert deps.score_stats.extract_deferred == {kind: 1}
    expected_attempts = 0 if kind == "credits/auth" else 1       # an outage burns no attempt
    assert it.extract_attempts == expected_attempts and not it.extract_failed


def test_credits_outage_halts_extract_and_scoring_for_the_run(conn, fake_fx):
    client = ExtractLLM(_credit_error())
    deps = _deps(client, fake_fx)
    a, b = _item(conn, "1"), _item(conn, "2", POST + " second")
    pipeline.advance_by_id(conn, a, deps=deps)
    assert deps.score_stats.halted
    res = pipeline.advance_by_id(conn, b, deps=deps)
    assert "halted" in res.reason and client.calls == 1            # no second call burned
    assert deps.score_stats.extract_skipped_halted == 1
    assert store.get_item(conn, b).state == "discovered"


def test_three_failures_keep_the_item_for_manual_review_never_scored_never_deleted(conn, fake_fx):
    client = ExtractLLM(_rate_limit(), _rate_limit(), _rate_limit())
    deps = _deps(client, fake_fx, extract_max_attempts=3)
    iid = _item(conn)
    for _ in range(3):
        pipeline.advance_by_id(conn, iid, deps=deps)
    it = store.get_item(conn, iid)
    assert it.extract_attempts == 3 and it.extract_failed and it.state == "discovered"
    assert deps.score_stats.extract_failed_final == 1
    assert store.list_by_state(conn, "discovered", include_score_failed=False) == []   # harvest skips it
    assert [i.id for i in store.list_by_state(conn, "discovered")] == [iid]
    res = pipeline.advance_by_id(conn, iid, deps=deps)                # even a direct advance does nothing
    assert res.status == "noop" and client.calls == 3


def test_recovery_after_a_deferral_extracts_with_the_model(conn, fake_fx):
    client = ExtractLLM(_rate_limit(), GOOD_JSON)
    deps = _deps(client, fake_fx)
    iid = _item(conn)
    pipeline.advance_by_id(conn, iid, deps=deps)                   # deferred
    pipeline.advance_by_id(conn, iid, deps=deps)                   # next harvest: model works
    it = store.get_item(conn, iid)
    assert it.state == "extracted" and it.extract_mode == "llm" and '"LLM Engineer"' in it.extracted_json


# --- deliberate heuristics are marked ----------------------------------------

def test_deliberate_heuristic_paths_are_marked(conn, fake_fx):
    deps = _deps(ExtractLLM(), fake_fx, topic_gate_enforce=True)
    junk = _item(conn, "1", "ищу работу python разработчик, резюме в личке всем привет")
    topic = _item(conn, "2", "Senior accountant needed in Warsaw office, Excel and 1C, full time position")
    for i in (junk, topic):
        pipeline.advance_by_id(conn, i, deps=deps)
    assert store.get_item(conn, junk).extract_mode == "heuristic:prefilter"
    assert store.get_item(conn, topic).extract_mode == "heuristic:topic"
    assert deps.score_stats.extract_attempted == 0                  # no model call was even tried


def test_model_extract_is_marked_llm(conn, fake_fx):
    deps = _deps(ExtractLLM(GOOD_JSON), fake_fx)
    iid = _item(conn)
    pipeline.advance_by_id(conn, iid, deps=deps)
    assert store.get_item(conn, iid).extract_mode == "llm"


def test_no_llm_dev_mode_is_marked_distinctly(conn, fake_fx):
    deps = Deps(fx=fake_fx, use_llm_extract=False)
    iid = _item(conn)
    pipeline.advance_by_id(conn, iid, deps=deps)
    assert store.get_item(conn, iid).extract_mode == "heuristic:no-llm"


# --- ops ---------------------------------------------------------------------

def test_extract_line_and_alert():
    s = pipeline.ScoreRunStats(extract_attempted=40)
    s.extract_deferred = {"rate-limit/5xx/timeout": 2, "parse": 1}
    s.extract_failed_final = 1
    assert run.extract_line(s) == "extract: deferred 3 (credits 0 / transient 2 / parse 1), failed(3x) 1"
    assert run.scoring_alert_text(s) is None                       # 3/40 = 7.5%: not red
    assert "extract: deferred 3" in run.parked_info_text(s)        # but visible in the info message
    s.extract_deferred = {"credits/auth": 1}
    s.extract_skipped_halted = 12
    text = run.scoring_alert_text(s)
    assert text.startswith("\U0001f534 jobhunter extract") and "not tried (credits/auth halt) 12" in text
    s2 = pipeline.ScoreRunStats(extract_attempted=10)
    s2.extract_deferred = {"parse": 3}                             # 30% > 20%
    assert run.scoring_alert_text(s2) is not None


def test_old_alert_behaviour_is_unchanged_and_now_carries_the_extract_line():
    s = pipeline.ScoreRunStats(attempted=46, failed=12, deferred=12)
    s.kinds["parse"] = 12
    t = run.scoring_alert_text(s)
    assert "12/46" in t and "extract: deferred 0" in t
