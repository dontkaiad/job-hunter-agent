"""FX pure conversion + cached fetch with mocked HTTP."""

import json

from job_hunter import fx as fx_mod
from job_hunter.fx import FxRates, convert_to_rub, format_rub


def test_convert_to_rub_pure():
    rates = {"RUB": 1.0, "USD": 90.0, "EUR": 100.0}
    assert convert_to_rub(1000, "USD", rates) == 90000
    assert convert_to_rub(1000, "RUB", rates) == 1000
    assert convert_to_rub(None, "USD", rates) is None
    assert convert_to_rub(1000, "JPY", rates) is None  # unknown
    assert convert_to_rub(1000, None, rates) is None


def test_format_rub():
    assert format_rub(290000) == "~290k ₽"
    assert format_rub(None) == "?"
    assert format_rub(500) == "~500 ₽"


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_frankfurter_fetch_inverts_rates_and_caches():
    calls = {"n": 0}

    def fake_get(url):
        calls["n"] += 1
        # frankfurter base=RUB: 1 RUB = 0.011 USD, 0.01 EUR
        return FakeResp({"rates": {"USD": 0.011, "EUR": 0.01}})

    fx = FxRates(provider="frankfurter", cache_ttl=86400, http_get=fake_get)
    rates = fx.get_rates_per_rub()
    # 1/0.011 ~= 90.9 RUB per USD
    assert abs(rates["USD"] - (1 / 0.011)) < 1e-6
    assert rates["RUB"] == 1.0

    # Second call hits cache, no new HTTP.
    fx.get_rates_per_rub()
    assert calls["n"] == 1

    assert abs(fx.convert(1000, "EUR") - 1000 * (1 / 0.01)) < 1e-6


def test_erapi_fetch():
    def fake_get(url):
        assert "er-api" in url
        return FakeResp({"rates": {"USD": 0.011}})

    fx = FxRates(provider="erapi", cache_ttl=86400, http_get=fake_get)
    rates = fx.get_rates_per_rub()
    assert "USD" in rates


def test_cache_expiry_refetches(monkeypatch):
    calls = {"n": 0}

    def fake_get(url):
        calls["n"] += 1
        return FakeResp({"rates": {"USD": 0.011}})

    fx = FxRates(provider="frankfurter", cache_ttl=0, http_get=fake_get)
    fx.get_rates_per_rub()
    fx.get_rates_per_rub()
    assert calls["n"] == 2  # ttl=0 -> never fresh
