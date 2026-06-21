"""Shared pytest fixtures: in-memory sqlite store + fakes for LLM/FX."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from job_hunter import store
from job_hunter.pipeline import Deps


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    yield c
    c.close()


class FakeLLM:
    """Records calls and returns scripted responses by call index/system text."""

    def __init__(self, responses: Optional[List[str]] = None):
        self.responses = list(responses or [])
        self.calls: List[Dict[str, Any]] = []
        self.by_system: Dict[str, str] = {}

    def set_for(self, system_substr: str, response: str) -> None:
        self.by_system[system_substr] = response

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        model=None,
        cache_system: bool = False,
    ) -> str:
        # Record the structured ``system`` shape the real transport would build,
        # so caching tests can assert on cache_control without a live client.
        from job_hunter.llm import build_system_param

        self.calls.append(
            {
                "system": system,
                "user": user,
                "max_tokens": max_tokens,
                "model": model,
                "cache_system": cache_system,
                "system_param": build_system_param(system, cache_system),
            }
        )
        for key, resp in self.by_system.items():
            if key in system:
                return resp
        if self.responses:
            return self.responses.pop(0)
        return "{}"


class FakeFx:
    """Static RUB-per-unit table; no network."""

    def __init__(self, rates: Optional[Dict[str, float]] = None):
        self.rates = rates or {"RUB": 1.0, "USD": 90.0, "EUR": 100.0}

    def convert(self, amount, currency):
        if amount is None or currency is None:
            return None
        code = currency.upper()
        if code == "RUB":
            return float(amount)
        rate = self.rates.get(code)
        return None if rate is None else float(amount) * rate

    def get_rates_per_rub(self):
        return dict(self.rates)


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def fake_fx():
    return FakeFx()


@pytest.fixture
def deps(fake_llm, fake_fx):
    # Default judge response: a clearly-relevant score so GOOD_POST-style items
    # surface in pipeline/bot tests. Individual tests override via set_for.
    fake_llm.set_for(
        "hiring-fit JUDGE",
        '{"relevance_score": 85, "Обоснование": "applied-LLM role, remote, good stack"}',
    )
    return Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
