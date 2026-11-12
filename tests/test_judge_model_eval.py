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
