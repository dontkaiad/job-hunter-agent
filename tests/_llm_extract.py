"""Test helper: make the fake LLM extraction return the salary that the post text states.

Since B1 the hard salary guard acts only on LLM-extracted rows (``extract_mode`` not ``heuristic%``). Tests that check
the guard therefore need the row to come through the LLM extraction path. The same post text is kept as the input; the
helper reads its salary with the heuristic extractor and hands exactly that to the fake LLM as its answer.
"""

from __future__ import annotations

import json

from job_hunter.extract import extract as heuristic_extract


def llm_extracts_salary_of(fake_llm, text: str, **extra) -> None:
    h = heuristic_extract(text, "@c", None)
    payload = {"title": "AI Engineer", "stack": ["python", "llm"], "remote": True,
               "salary_min": h.salary_min, "salary_max": h.salary_max, "currency": h.currency, **extra}
    fake_llm.set_for("strict JSON", json.dumps(payload))
