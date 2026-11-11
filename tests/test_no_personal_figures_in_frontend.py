"""The public frontend must carry no candidate-specific market figures (they live in the private profile)."""
import os
import re

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "src")
FORBIDDEN = [r"не ниже", r"целиться", r"метить\s",              # advice wording
             r"\d[\d\s]{3,}\s*[–-]\s*\d[\d\s]{2,}\s*₽",         # a hard-coded ruble range
             r"[$€]\s?\d[\d\s.,]*\s*[–-]\s*[$€]?\s?\d"]          # a hard-coded dollar/euro range


def test_market_page_has_no_hardcoded_benchmark_figures_or_advice():
    text = open(os.path.join(SRC, "views", "MarketWorthView.jsx"), encoding="utf-8").read()
    for pat in FORBIDDEN:
        assert not re.search(pat, text), f"personal benchmark text back in the public frontend: {pat}"
