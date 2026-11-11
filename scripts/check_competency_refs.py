#!/usr/bin/env python3
"""Check every competency ``source_ref`` against the real repos. REPORT ONLY: never edits anything.

A competency evidence row points at code as ``path[:LINE (symbol)[, LINE (symbol)]][; more paths]``.
Line numbers drift after refactors; this script finds out where each pointer stands NOW:

  OK              file exists and the symbol is on the stated line
  DRIFT           file exists, the symbol is elsewhere (the report gives the current line)
  MISSING_SYMBOL  file exists, the symbol is not in it (a descriptive note in the parentheses
                  also lands here: read the row, it is a hint, not always a bug)
  MISSING_FILE    the path (or glob) matches nothing
  NO_FILE         a path-only pointer (no line, no symbol) whose file exists: nothing finer to check
  NO_PUBLIC_CODE  the row says "n/a" (work at an employer / NDA): honest absence, not "unchecked"

Repo roots come from the environment, one variable per project name in the YAML:
``COMPETENCY_REPO_<PROJECT>`` with the project name upper-cased and ``-`` -> ``_``
(e.g. ``COMPETENCY_REPO_MY_PROJECT=/path/to/my-project``). When a variable is unset the
project is looked up as a sibling directory of this repo (``<parent>/<project>``).

    python scripts/check_competency_refs.py [--file config/competencies.local.yaml] [--strict]

``--strict`` exits 1 when anything is MISSING_FILE / MISSING_SYMBOL / DRIFT (default: always 0).
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from typing import Dict, Iterator, List, Optional, Tuple

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_FILE = os.path.join(REPO_ROOT, "config", "competencies.local.yaml")
BUCKETS = ("core", "growing")

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


def repo_root(project: str, env: Optional[Dict[str, str]] = None) -> str:
    """Where ``project`` lives: COMPETENCY_REPO_<PROJECT>, else a sibling of this repo. PURE."""
    env = os.environ if env is None else env
    key = "COMPETENCY_REPO_" + re.sub(r"[^A-Z0-9]+", "_", project.upper())
    return env.get(key) or os.path.join(os.path.dirname(REPO_ROOT), project)


def _top_level_paren(text: str) -> str:
    """Content of the first balanced (...) group in ``text`` ('' if none). PURE."""
    start = text.find("(")
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    return text[start + 1:]


def _symbol(note: str) -> Optional[str]:
    """The identifier to grep for from a parenthesised note; dotted names use the last segment. PURE."""
    m = _IDENT.search(note)
    if not m:
        return None
    return m.group(0).rstrip(".").split(".")[-1] or None


_PATHLIKE = re.compile(r"[\w*./-]+\.[A-Za-z]{1,5}\b|[\w*.-]+/")


def _split_paths(ref: str) -> List[str]:
    """Split a ref into per-path parts: on ';' and on a top-level ',' that starts a new path. PURE."""
    parts, depth, cur, i = [], 0, "", 0
    while i < len(ref):
        ch = ref[i]
        depth += (ch == "(") - (ch == ")")
        if depth == 0 and (ch == ";" or (ch == "," and _PATHLIKE.match(ref[i + 1:].lstrip()))):
            parts.append(cur)
            cur = ""
        else:
            cur += ch
        i += 1
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def parse_ref(ref: str) -> List[Tuple[str, List[Tuple[Optional[int], Optional[str]]]]]:
    """``source_ref`` -> [(path, [(line|None, symbol|None), ...]), ...]. PURE."""
    out = []
    for part in _split_paths(ref):
        m = re.match(r"^([^\s:(]+)(.*)$", part)
        if not m:
            out.append((part, []))
            continue
        path, rest = m.group(1), m.group(2).strip()
        if rest.startswith(":"):
            rest = rest[1:].strip()
        elif rest.startswith("("):
            rest = "(" + rest[1:]          # path (note): no line, the note may still name a symbol
        hints: List[Tuple[Optional[int], Optional[str]]] = []
        if rest:
            for chunk in re.split(r",\s*(?=\d)", rest):
                chunk = chunk.strip()
                lm = re.match(r"^(\d+)(?:-\d+)?", chunk)
                line = int(lm.group(1)) if lm else None
                hints.append((line, _symbol(_top_level_paren(chunk))))
        out.append((path, hints))
    return out


def _symbol_lines(path: str, symbol: str) -> List[int]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [n for n, line in enumerate(fh, 1) if symbol in line]
    except OSError:
        return []


def check_pointer(root: str, path: str, hints) -> List[Tuple[str, str]]:
    """Status rows [(status, detail)] for one path of one ref. Reads files only."""
    matches = sorted(glob.glob(os.path.join(root, path), recursive=True))
    if not matches:
        return [("MISSING_FILE", path)]
    if not hints:
        return [("NO_FILE", path)]
    target = matches[0]
    rows = []
    for line, symbol in hints:
        if symbol is None:
            rows.append(("NO_FILE", f"{path}:{line}" if line else path))
            continue
        found = _symbol_lines(target, symbol)
        label = f"{path}:{line} ({symbol})" if line else f"{path} ({symbol})"
        if not found:
            rows.append(("MISSING_SYMBOL", label))
        elif line is None or line in found:
            rows.append(("OK", label))
        else:
            nearest = min(found, key=lambda n: abs(n - line))
            rows.append(("DRIFT", f"{label} -> now line {nearest}"))
    return rows


def iter_entries(data: dict) -> Iterator[Tuple[str, str, dict]]:
    for bucket in BUCKETS:
        for entry in data.get(bucket) or []:
            for ev in entry.get("evidence") or []:
                yield bucket, entry.get("term_en") or entry.get("term_ru") or "?", ev


def check_all(data: dict, env: Optional[Dict[str, str]] = None) -> List[Tuple[str, str, str, str, str]]:
    """[(bucket, term, project, status, detail)] for every evidence row. Reads files only."""
    rows = []
    for bucket, term, ev in iter_entries(data):
        project, ref = ev.get("project", ""), str(ev.get("source_ref") or "")
        if ref.strip().lower().startswith("n/a"):
            rows.append((bucket, term, project, "NO_PUBLIC_CODE", ref))
            continue
        root = repo_root(project, env)
        if not os.path.isdir(root):
            rows.append((bucket, term, project, "MISSING_FILE", f"repo not found: {root}"))
            continue
        for path, hints in parse_ref(ref):
            for status, detail in check_pointer(root, path, hints):
                rows.append((bucket, term, project, status, detail))
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=DEFAULT_FILE)
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    with open(args.file, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    rows = check_all(data)
    counts: Dict[str, int] = {}
    for bucket, term, project, status, detail in rows:
        counts[status] = counts.get(status, 0) + 1
        if status not in ("OK", "NO_FILE"):
            print(f"{status:15} [{bucket}] {term[:48]} | {project} | {detail}")
    print("\nsummary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())), f"(rows={len(rows)})")
    bad = sum(counts.get(k, 0) for k in ("MISSING_FILE", "MISSING_SYMBOL", "DRIFT"))
    return 1 if (args.strict and bad) else 0


if __name__ == "__main__":
    sys.exit(main())
