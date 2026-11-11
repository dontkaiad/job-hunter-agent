"""The public frontend must carry no candidate-specific market figures (they live in the private profile)."""
import os
import re

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "src")
FORBIDDEN = [r"не ниже", r"целиться", r"метить\s", r"\b200\s?000\b", r"\b300\s?000\b", r"4\s?500\s?[–-]\s?8\s?000"]


def test_market_page_has_no_hardcoded_benchmark_figures_or_advice():
    text = open(os.path.join(SRC, "views", "MarketWorthView.jsx"), encoding="utf-8").read()
    for pat in FORBIDDEN:
        assert not re.search(pat, text), f"personal benchmark text back in the public frontend: {pat}"
