"""Live FX conversion to RUB-equivalent, no API key, cached 24h.

I/O module: fetches rates from a free no-key source (frankfurter.app or
open.er-api.com) via httpx and caches them in memory for ``cache_ttl`` seconds.

The pure conversion math (``convert_to_rub``) takes an explicit rate table so it
is unit-testable without the network. The cache uses clock.now_utc().
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional

import httpx

from .clock import now_utc

FRANKFURTER_URL = "https://api.frankfurter.app/latest"
ERAPI_URL = "https://open.er-api.com/v6/latest/{base}"

# Symbols accepted as already-RUB.
_RUB = "RUB"


def convert_to_rub(
    amount: Optional[float],
    currency: Optional[str],
    rates_per_rub: Dict[str, float],
) -> Optional[float]:
    """Convert ``amount`` in ``currency`` to RUB. PURE.

    ``rates_per_rub`` maps a currency code -> how many RUB one unit is worth
    (e.g. {"USD": 90.0, "EUR": 98.0, "RUB": 1.0}). Returns None when the amount
    is None or the currency is unknown/unconvertible.
    """
    if amount is None:
        return None
    if currency is None:
        # No currency given: assume the number is already in RUB only if the
        # caller wants that. Here we cannot know, so return None (unknown).
        return None
    code = currency.upper()
    if code == _RUB:
        return float(amount)
    rate = rates_per_rub.get(code)
    if rate is None:
        return None
    return float(amount) * rate


@dataclass
class _CacheEntry:
    rates_per_rub: Dict[str, float]
    fetched_epoch: float


class FxRates:
    """Fetches and caches RUB-denominated rates.

    Stores rates as RUB-per-unit-of-foreign-currency so that
    ``convert_to_rub(amount, ccy, rates)`` is a simple multiply.
    """

    def __init__(
        self,
        provider: str = "frankfurter",
        cache_ttl: int = 86400,
        http_get: Optional[Callable[[str], "httpx.Response"]] = None,
    ) -> None:
        self.provider = provider
        self.cache_ttl = cache_ttl
        # Injectable for tests; defaults to a real httpx GET.
        self._http_get = http_get or self._default_get
        self._cache: Optional[_CacheEntry] = None

    @staticmethod
    def _default_get(url: str) -> "httpx.Response":
        return httpx.get(url, timeout=15.0)

    def _is_fresh(self) -> bool:
        if self._cache is None:
            return False
        age = now_utc().timestamp() - self._cache.fetched_epoch
        return age < self.cache_ttl

    def get_rates_per_rub(self) -> Dict[str, float]:
        """Return cached (or freshly fetched) RUB-per-unit rates."""
        if self._is_fresh():
            return dict(self._cache.rates_per_rub)  # type: ignore[union-attr]
        rates = self._fetch()
        self._cache = _CacheEntry(rates_per_rub=rates, fetched_epoch=now_utc().timestamp())
        return dict(rates)

    def _fetch(self) -> Dict[str, float]:
        if self.provider == "erapi":
            return self._fetch_erapi()
        return self._fetch_frankfurter()

    def _fetch_frankfurter(self) -> Dict[str, float]:
        """frankfurter.app: base=RUB gives RUB->X. We invert to X->RUB."""
        url = f"{FRANKFURTER_URL}?from={_RUB}"
        resp = self._http_get(url)
        resp.raise_for_status()
        data = resp.json()
        # data["rates"] = {"USD": 0.011, "EUR": 0.0102, ...} meaning 1 RUB = X.
        out: Dict[str, float] = {_RUB: 1.0}
        for code, rub_to_x in data.get("rates", {}).items():
            if rub_to_x:
                out[code.upper()] = 1.0 / float(rub_to_x)
        return out

    def _fetch_erapi(self) -> Dict[str, float]:
        """open.er-api.com: base=RUB gives 1 RUB = X foreign. Invert to X->RUB."""
        url = ERAPI_URL.format(base=_RUB)
        resp = self._http_get(url)
        resp.raise_for_status()
        data = resp.json()
        out: Dict[str, float] = {_RUB: 1.0}
        for code, rub_to_x in (data.get("rates") or {}).items():
            if rub_to_x:
                out[code.upper()] = 1.0 / float(rub_to_x)
        return out

    def convert(self, amount: Optional[float], currency: Optional[str]) -> Optional[float]:
        """Convert using current (cached) rates."""
        if amount is None or currency is None:
            return None
        if currency.upper() == _RUB:
            return float(amount)
        return convert_to_rub(amount, currency, self.get_rates_per_rub())


def format_rub(amount_rub: Optional[float]) -> str:
    """Render a RUB amount as a short 'k' string, e.g. 290000 -> '~290k ₽'."""
    if amount_rub is None:
        return "?"
    if amount_rub >= 1000:
        return f"~{round(amount_rub / 1000)}k ₽"
    return f"~{round(amount_rub)} ₽"
