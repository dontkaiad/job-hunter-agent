"""Spending guard for every script that calls the API outside the harvest.

Rules (operator, 2026-11-08): no --max-usd -> no start; a cost estimate from the real
input sizes is printed first and nothing is spent without --confirm; the run stops at
its limit; the end-of-run line reports input / cache write / cache read / output and $.
"""
import inspect
import pathlib
import re
import types

import pytest

from evals import common, cost_probe, golden_eval, link_fetch_eval, parse_robustness_eval, score_stability_eval

EVALS = pathlib.Path(common.__file__).parent


@pytest.fixture(autouse=True)
def _clean_gate():
    common._GATE.clear()
    yield
    common._GATE.clear()


# --- the flags --------------------------------------------------------------

@pytest.mark.parametrize("mod,argv", [
    (score_stability_eval, ["run", "--ids", "1"]),
    (parse_robustness_eval, ["run", "--ids", "1"]),
    (link_fetch_eval, ["run"]),
    (cost_probe, ["run"]),
    (golden_eval, ["frame"]),
])
def test_live_modes_refuse_to_start_without_max_usd(mod, argv, capsys):
    with pytest.raises(SystemExit) as e:
        mod.main(argv)
    assert e.value.code == 2 and "--max-usd" in capsys.readouterr().err


# --- gate -------------------------------------------------------------------

def test_gate_requires_a_positive_limit():
    for bad in (None, 0, -1):
        with pytest.raises(SystemExit, match="--max-usd"):
            common.gate("t", 0.10, bad, True)
    assert not common._GATE


def test_gate_refuses_when_the_estimate_exceeds_the_limit(capsys):
    with pytest.raises(SystemExit, match="exceeds"):
        common.gate("t", 2.50, 1.00, True)
    assert not common._GATE


def test_gate_without_confirm_prints_the_estimate_and_spends_nothing(capsys):
    with pytest.raises(SystemExit) as e:
        common.gate("score_stability", 0.62, 1.00, False)
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "ESTIMATE $0.62" in out and "NO API call was made" in out and "--confirm" in out
    assert not common._GATE


def test_gate_with_confirm_opens_the_run():
    common.gate("t", 0.62, 1.00, True)
    assert common._GATE == {"label": "t", "max_usd": 1.0}


def test_no_client_without_a_passed_gate():
    cfg = types.SimpleNamespace(database_url="postgresql://x", anthropic_api_key="k", cheap_model="claude-haiku-4-5")
    with pytest.raises(RuntimeError, match="gate"):
        common.make_client(cfg, "x")


def test_only_make_client_creates_api_clients_in_evals():
    for f in EVALS.glob("*.py"):
        if f.name == "common.py":
            continue
        assert "AnthropicClient(" not in f.read_text(encoding="utf-8"), f.name
    for fn in (golden_eval.run_sample, golden_eval.run_estimate_labeled):      # the no-API steps
        src = inspect.getsource(fn)
        assert "make_client" not in src and "AnthropicClient" not in src and "gate(" not in src


# --- the limit --------------------------------------------------------------

class _Msgs:
    def __init__(self, usd_in=1_000_000):
        self.n, self.usage_in = 0, usd_in

    def create(self, **kw):
        self.n += 1
        return types.SimpleNamespace(usage=types.SimpleNamespace(
            input_tokens=self.usage_in, output_tokens=0, cache_creation_input_tokens=0,
            cache_read_input_tokens=0))


def test_meter_stops_before_the_call_that_would_exceed_the_limit():
    msgs = _Msgs()                                   # 1M Haiku input tokens = $1.00 per call
    client = types.SimpleNamespace(_client=types.SimpleNamespace(messages=msgs))
    meter = common.UsageMeter(max_usd=2.50).attach(client)
    for _ in range(3):
        client._client.messages.create(model="claude-haiku-4-5")
    with pytest.raises(common.BudgetExceeded):
        client._client.messages.create(model="claude-haiku-4-5")
    assert msgs.n == 3                                # the 4th call never reached the API
    assert meter.spent_usd() == pytest.approx(3.0)    # overshoot is at most one call


def test_meter_without_a_limit_never_stops_and_summarises_tokens():
    msgs = _Msgs(usd_in=1000)
    client = types.SimpleNamespace(_client=types.SimpleNamespace(messages=msgs))
    meter = common.UsageMeter().attach(client)
    for _ in range(5):
        client._client.messages.create(model="claude-haiku-4-5")
    r = meter.report()
    assert r["calls"] == 5 and r["tokens_total"] == {"input": 5000, "cache_write": 0, "cache_read": 0, "output": 0}


def test_thread_cost_is_per_item(capsys):
    msgs = _Msgs(usd_in=1_000_000)
    client = types.SimpleNamespace(_client=types.SimpleNamespace(messages=msgs))
    meter = common.UsageMeter().attach(client)
    meter.thread_cost_reset()
    client._client.messages.create(model="claude-haiku-4-5")
    client._client.messages.create(model="claude-haiku-4-5")
    assert meter.thread_cost() == pytest.approx(2.0)
    meter.thread_cost_reset()
    assert meter.thread_cost() == 0.0
    common.print_usage_summary(meter)
    assert "input=2000000 cache_write=0 cache_read=0 output=0 -> $2.0000" in capsys.readouterr().out


def test_eval_usage_is_logged_to_llm_usage_under_an_eval_step(monkeypatch):
    seen = []
    from job_hunter import store
    monkeypatch.setattr(store, "make_usage_sink", lambda dsn: seen.append)
    common.gate("t", 0.1, 1.0, True)
    cfg = types.SimpleNamespace(database_url="postgresql://x", anthropic_api_key="k", cheap_model="claude-haiku-4-5")
    client = common.make_client(cfg, "golden_frame")
    client._record_usage(types.SimpleNamespace(usage=types.SimpleNamespace(
        input_tokens=5, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0)),
        "claude-haiku-4-5")
    assert seen and seen[0]["step"] == "eval:golden_frame:other" and seen[0]["input_tokens"] == 5


# --- the estimate -----------------------------------------------------------

def test_estimate_is_calibrated_and_batch_is_half():
    sysm, user = "словo " * 500, "вакансия " * 300
    full = common.est_call_usd("claude-haiku-4-5", sysm, user, 420)
    assert common.est_call_usd("claude-haiku-4-5", sysm, user, 420, batch=True) == pytest.approx(full / 2)
    toks = common.est_tokens(sysm) + common.est_tokens(user)
    expect = (toks * common.TOKEN_CALIBRATION * 1.0 + 420 * 5.0) / 1e6
    assert full == pytest.approx(expect)
    with pytest.raises(ValueError):
        common.est_call_usd("some-future-model", "a", "b", 1)


# --- the docs use the guarded commands ---------------------------------------

def test_documented_live_commands_carry_max_usd():
    root = EVALS.parent
    text = "\n".join(p.read_text(encoding="utf-8") for p in (root / "docs" / "CASES").glob("*.md"))
    cmds = re.findall(r"python -m evals\.\w+ (?:run|frame)[^\n`]*", text)
    assert cmds and all("--max-usd" in c for c in cmds), [c for c in cmds if "--max-usd" not in c]
