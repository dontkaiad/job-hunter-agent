"""Tests for job_hunter.market_worth — no real API calls, no file I/O.

Uses:
- _caller seam to bypass web_search
- tmp_path to isolate cache files
- SimpleNamespace / dataclass-like fakes for cfg and profile
"""

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from job_hunter.market_worth import (
    MarketWorthResult,
    _extract_json,
    _parse_and_validate,
    _validate,
    age_days,
    fmt_range,
    get_or_refresh,
    is_stale,
    load_cache,
    save_cache,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cfg(tmp_path, **overrides):
    defaults = dict(
        anthropic_api_key="test-key",
        market_model="claude-sonnet-4-6",
        market_worth_cache_path=str(tmp_path / "mw.json"),
        market_worth_cache_days=14,
        market_worth_ru_min=50_000,
        market_worth_ru_max=700_000,
        market_worth_intl_min=1_000,
        market_worth_intl_max=15_000,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _profile():
    return SimpleNamespace(
        role="AI/Prompt Engineer",
        target_grade="middle",
        hands_on=["prompt engineering", "RAG"],
        stack=["Python", "LangChain"],
        location_priority=["remote"],
    )


def _good_json():
    return json.dumps(
        {
            "ru_min": 150_000,
            "ru_max": 250_000,
            "ru_currency": "RUB",
            "intl_min": 3_000,
            "intl_max": 6_000,
            "intl_currency": "EUR",
            "sources": ["https://hh.ru/example", "Glassdoor: AI Engineer salaries"],
            "reasoning_short": "Middle AI engineers in Russia earn 150-250k ₽/month.",
        }
    )


def _make_result(**overrides):
    base = dict(
        ru_min=150_000, ru_max=250_000, ru_currency="RUB",
        intl_min=3_000, intl_max=6_000, intl_currency="EUR",
        sources=["https://hh.ru/example"],
        reasoning_short="Test reasoning.",
        computed_at=datetime.now(timezone.utc).isoformat(),
    )
    base.update(overrides)
    return MarketWorthResult(**base)


# ---------------------------------------------------------------------------
# T1: _extract_json
# ---------------------------------------------------------------------------


def test_extract_json_bare():
    raw = '{"a": 1}'
    assert _extract_json(raw) == {"a": 1}


def test_extract_json_with_prose():
    raw = 'Here is the data:\n{"ru_min": 100000}\nDone.'
    assert _extract_json(raw) == {"ru_min": 100000}


def test_extract_json_code_fence():
    raw = "```json\n{\"x\": 42}\n```"
    assert _extract_json(raw) == {"x": 42}


def test_extract_json_raises_on_no_json():
    with pytest.raises(ValueError, match="no JSON"):
        _extract_json("no json here at all")


# ---------------------------------------------------------------------------
# T2: _validate — sanity checks
# ---------------------------------------------------------------------------


def test_validate_clean(tmp_path):
    cfg = _cfg(tmp_path)
    result = _make_result()
    out = _validate(result, cfg)
    assert not out.degraded


def test_validate_no_sources(tmp_path):
    cfg = _cfg(tmp_path)
    result = _make_result(sources=[])
    out = _validate(result, cfg)
    assert out.degraded
    assert "sources" in out.degraded_reason


def test_validate_ru_min_exceeds_max(tmp_path):
    cfg = _cfg(tmp_path)
    result = _make_result(ru_min=300_000, ru_max=200_000)
    out = _validate(result, cfg)
    assert out.degraded
    assert "ru_min" in out.degraded_reason


def test_validate_ru_range_out_of_bounds(tmp_path):
    cfg = _cfg(tmp_path, market_worth_ru_max=700_000)
    result = _make_result(ru_min=50_000, ru_max=800_000)
    out = _validate(result, cfg)
    assert out.degraded
    assert "bounds" in out.degraded_reason


def test_validate_intl_min_exceeds_max(tmp_path):
    cfg = _cfg(tmp_path)
    result = _make_result(intl_min=9_000, intl_max=2_000)
    out = _validate(result, cfg)
    assert out.degraded
    assert "intl_min" in out.degraded_reason


def test_validate_intl_out_of_bounds(tmp_path):
    cfg = _cfg(tmp_path, market_worth_intl_max=15_000)
    result = _make_result(intl_min=5_000, intl_max=20_000)
    out = _validate(result, cfg)
    assert out.degraded


# ---------------------------------------------------------------------------
# T3: _parse_and_validate — full round-trip
# ---------------------------------------------------------------------------


def test_parse_valid(tmp_path):
    cfg = _cfg(tmp_path)
    result = _parse_and_validate(_good_json(), cfg)
    assert result.ru_min == 150_000
    assert result.ru_max == 250_000
    assert result.intl_currency == "EUR"
    assert len(result.sources) == 2
    assert not result.degraded


def test_parse_degraded_when_bad_range(tmp_path):
    cfg = _cfg(tmp_path)
    bad = json.dumps({
        "ru_min": 300_000, "ru_max": 100_000, "ru_currency": "RUB",
        "intl_min": 3_000, "intl_max": 6_000, "intl_currency": "EUR",
        "sources": ["x"],
        "reasoning_short": "ok",
    })
    result = _parse_and_validate(bad, cfg)
    assert result.degraded


# ---------------------------------------------------------------------------
# T4: cache round-trip
# ---------------------------------------------------------------------------


def test_save_and_load(tmp_path):
    cfg = _cfg(tmp_path)
    result = _make_result()
    save_cache(result, cfg.market_worth_cache_path)
    loaded = load_cache(cfg.market_worth_cache_path)
    assert loaded is not None
    assert loaded.ru_min == result.ru_min
    assert loaded.sources == result.sources


def test_load_missing_cache(tmp_path):
    assert load_cache(str(tmp_path / "nonexistent.json")) is None


def test_load_corrupt_cache(tmp_path):
    p = tmp_path / "corrupt.json"
    p.write_text("NOT JSON")
    assert load_cache(str(p)) is None


# ---------------------------------------------------------------------------
# T5: is_stale / age_days
# ---------------------------------------------------------------------------


def test_is_stale_fresh(tmp_path):
    result = _make_result()
    assert not is_stale(result, max_age_days=14)


def test_is_stale_old(tmp_path):
    old_ts = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    result = _make_result(computed_at=old_ts)
    assert is_stale(result, max_age_days=14)


def test_age_days_today():
    result = _make_result()
    assert age_days(result) == 0


def test_age_days_old():
    ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    result = _make_result(computed_at=ts)
    assert age_days(result) == 3


# ---------------------------------------------------------------------------
# T6: get_or_refresh — cache hit (no _caller call)
# ---------------------------------------------------------------------------


def test_get_or_refresh_uses_cache(tmp_path):
    cfg = _cfg(tmp_path)
    cached = _make_result()
    save_cache(cached, cfg.market_worth_cache_path)

    called = []

    def fake_caller(prompt):
        called.append(prompt)
        return _good_json()

    result = get_or_refresh(cfg, _profile(), _caller=fake_caller)
    assert result.ru_min == cached.ru_min
    assert called == []  # cache hit — no LLM call


# ---------------------------------------------------------------------------
# T7: get_or_refresh — stale cache triggers refresh
# ---------------------------------------------------------------------------


def test_get_or_refresh_stale_triggers_refresh(tmp_path):
    cfg = _cfg(tmp_path, market_worth_cache_days=14)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    old = _make_result(ru_min=99_000, computed_at=old_ts)
    save_cache(old, cfg.market_worth_cache_path)

    def fake_caller(prompt):
        return _good_json()

    result = get_or_refresh(cfg, _profile(), _caller=fake_caller)
    assert result.ru_min == 150_000  # new data


# ---------------------------------------------------------------------------
# T8: get_or_refresh — force=True bypasses fresh cache
# ---------------------------------------------------------------------------


def test_get_or_refresh_force(tmp_path):
    cfg = _cfg(tmp_path)
    fresh = _make_result(ru_min=99_000)
    save_cache(fresh, cfg.market_worth_cache_path)

    def fake_caller(prompt):
        return _good_json()

    result = get_or_refresh(cfg, _profile(), force=True, _caller=fake_caller)
    assert result.ru_min == 150_000


# ---------------------------------------------------------------------------
# T9: get_or_refresh — refresh fails, stale cache returned degraded
# ---------------------------------------------------------------------------


def test_get_or_refresh_failure_returns_stale_degraded(tmp_path):
    cfg = _cfg(tmp_path, market_worth_cache_days=14)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    old = _make_result(ru_min=99_000, computed_at=old_ts)
    save_cache(old, cfg.market_worth_cache_path)

    def bad_caller(prompt):
        raise RuntimeError("network down")

    result = get_or_refresh(cfg, _profile(), _caller=bad_caller)
    assert result.ru_min == 99_000
    assert result.degraded
    assert "network down" in result.degraded_reason


# ---------------------------------------------------------------------------
# T10: get_or_refresh — no cache + refresh fails → propagates
# ---------------------------------------------------------------------------


def test_get_or_refresh_no_cache_failure_raises(tmp_path):
    cfg = _cfg(tmp_path)

    def bad_caller(prompt):
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError, match="network down"):
        get_or_refresh(cfg, _profile(), _caller=bad_caller)


# ---------------------------------------------------------------------------
# T11: fmt_range helper
# ---------------------------------------------------------------------------


def test_fmt_range_both():
    assert "150" in fmt_range(150_000, 250_000, "RUB")
    assert "₽" in fmt_range(150_000, 250_000, "RUB")


def test_fmt_range_min_only():
    assert "от" in fmt_range(3_000, None, "EUR")
    assert "€" in fmt_range(3_000, None, "EUR")


def test_fmt_range_none():
    assert fmt_range(None, None, "EUR") == "—"
