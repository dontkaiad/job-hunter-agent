"""Stage 2b eval: failure kinds must stay distinguishable (a refused call is NOT a parse error)."""

from evals import judge_model_eval as j
from job_hunter import llm


def test_insufficient_is_a_normal_outcome():
    assert j.classify(llm.InsufficientContent("thin"))["outcome"] == "insufficient"


def test_parse_errors_are_split_by_kind():
    assert j.classify(ValueError("no tool_use block in response (stop_reason=end_turn)")) == {
        "outcome": "parse_error", "detail": "no_tool_use",
        "message": "no tool_use block in response (stop_reason=end_turn)"}
    assert j.classify(ValueError("tool call truncated by max_tokens"))["detail"] == "truncated"
    assert j.classify(ValueError("score not numeric: 'x'"))["detail"] == "not_numeric"


def test_api_refusal_is_not_a_parse_error():
    class Refused(Exception):
        status_code = 400

    rec = j.classify(Refused('tool_choice: type "tool" and "any" are not supported for this model.'))
    assert rec["outcome"] == "api_error" and "400" in rec["detail"]


def test_sonnet_5_5_is_priced_for_the_budget_meter():
    from evals import common
    assert common.price_for("claude-sonnet-5-5") is not None


class _Block:
    def __init__(self, type_, name="submit_score", input_=None):
        self.type, self.name, self.input = type_, name, input_ or {}


class _Resp:
    def __init__(self, blocks, stop="tool_use"):
        self.content, self.stop_reason = blocks, stop


class _Messages:
    def __init__(self, resp):
        self.resp, self.kwargs = resp, None

    def create(self, **kw):
        self.kwargs = kw
        return self.resp


class _FakeClient:
    def __init__(self, resp):
        self._client = type("C", (), {})()
        self._client.messages = _Messages(resp)
        self.recorded = 0

    def _record_usage(self, resp, model):
        self.recorded += 1


def test_auto_call_sends_tool_choice_auto_and_omits_temperature_when_none():
    c = _FakeClient(_Resp([_Block("tool_use", input_={"relevance_score": 70})]))
    out = j.score_tool_auto(c, "sys", "user", "claude-sonnet-5-5", temperature=None)
    kw = c._client.messages.kwargs
    assert out == {"relevance_score": 70} and kw["tool_choice"] == {"type": "auto"} and "temperature" not in kw
    assert c.recorded == 1


def test_auto_call_prose_answer_is_a_parse_error_not_an_api_error():
    c = _FakeClient(_Resp([_Block("text")], stop="end_turn"))
    try:
        j.score_tool_auto(c, "sys", "user", "claude-sonnet-5-5")
    except ValueError as exc:
        assert j.classify(exc)["outcome"] == "parse_error" and j.classify(exc)["detail"] == "no_tool_use"
    else:
        raise AssertionError("a prose reply must raise")
