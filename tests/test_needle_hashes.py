"""Hash-based de-identification guard that works in CI.

The personal strings live only in the maintainer's gitignored tests/_personal_needles.local.py, so
on a clean checkout those guards pass vacuously. This test closes that gap with a committed list of
SHA-256 hashes of the strings (tests/_needle_hashes.json; the strings themselves are NOT stored):
every 1-3 word n-gram of the public prompts, docs, examples and prompt-building code is hashed and
looked up, so a personal term that leaks into a public file fails the build without the repo ever
containing it."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HASHES = set(json.loads((ROOT / "tests" / "_needle_hashes.json").read_text(encoding="utf-8"))["hashes"])
TOKEN = re.compile(r"[\w][\w.'\-]*")
PUBLIC_PREFIXES = ("docs/", "evals/", "scripts/", "tests/fixtures/", "frontend/src/")
PUBLIC_FILES = {"job_hunter/llm.py", "job_hunter/profile.py", "job_hunter/config.py", "job_hunter/bot.py",
                "job_hunter/pipeline.py", "job_hunter/geo.py", ".env.example", "README.md", "SCORING.md",
                "ARCHITECTURE.md", "DESIGN.md", "DECISIONS.md", "COST.md", "CHANGELOG.md"}


def _norm_tokens(text: str):
    return [t.strip(".'-_") for t in TOKEN.findall(text.lower()) if t.strip(".'-_")]


def leaked(text: str, hashes=HASHES):
    """The hashes of any 1-3 word n-gram of ``text`` that is on the list. PURE."""
    toks = _norm_tokens(text)
    found = set()
    for n in (1, 2, 3):
        for i in range(len(toks) - n + 1):
            h = hashlib.sha256(" ".join(toks[i:i + n]).encode()).hexdigest()
            if h in hashes:
                found.add(h)
    return found


def _public_paths():
    out = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).split("\n")
    for f in out:
        if not f or f.endswith((".png", ".ico", ".svg", ".lock")) or f.endswith("package-lock.json"):
            continue
        if f in PUBLIC_FILES or f.startswith(PUBLIC_PREFIXES) or (f.startswith("config/") and f.endswith("example.yaml")):
            yield f


def test_the_guard_is_not_vacuous():
    assert len(HASHES) >= 10
    probe = {hashlib.sha256(b"two words").hexdigest()}
    assert leaked("A line with Two  Words, inside.", probe) == probe
    assert leaked("nothing here", probe) == set()


def test_no_personal_term_in_public_files():
    bad = {}
    for f in _public_paths():
        p = ROOT / f
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        hit = leaked(text)
        if hit:
            bad[f] = len(hit)
    assert not bad, f"personal terms (by hash) in public files: {bad}"


def test_no_personal_term_in_the_rendered_prompts():
    from job_hunter import llm
    from job_hunter.profile import example_profile
    from job_hunter.schema_extract import ExtractResult

    p = example_profile()
    texts = [llm.build_score_system(p, tool=t, version=v) for t in (True, False) for v in llm.SCORE_PROMPT_VERSIONS]
    texts.append(llm.build_draft_system(p))
    texts.append(llm.build_draft_prompt(ExtractResult(title="T", source_channel="@c"), "raw", None, gender="female"))
    for t in texts:
        assert not leaked(t)
