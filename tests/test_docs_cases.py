"""The case write-ups share one template, their links work, and Case 0006 quotes exactly what the
committed baselines say (rounding: values are printed as stored, 3 decimals; percents round half up)."""
from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CASES = sorted((ROOT / "docs" / "CASES").glob("0*.md"))
REQUIRED = ("## Summary", "## Methodology", "## Results", "## Limitations", "## Cost", "## Reproduce")


def _pct(x: float) -> str:
    return str(int(Decimal(str(x * 100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def _rng(lo: float, hi: float) -> str:
    return f"{lo:.3f}–{hi:.3f}"


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.name[:4])
def test_every_case_has_the_template_sections(path):
    text = path.read_text(encoding="utf-8")
    missing = [h for h in REQUIRED if h not in text]
    if path.name.startswith("0003"):          # a postmortem keeps its own structure and adds the mandatory ones
        missing = [h for h in missing if h != "## Results"]
    assert not missing, f"{path.name} lacks {missing}"


def test_cases_are_numbered_without_gaps_and_listed_in_the_index():
    nums = [int(p.name[:4]) for p in CASES]
    assert nums == list(range(1, len(nums) + 1))
    index = (ROOT / "docs" / "CASES" / "README.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for p in CASES:
        assert p.name in index, f"{p.name} missing from docs/CASES/README.md"
        assert p.name in readme, f"{p.name} missing from the root README"


def test_relative_links_in_the_docs_resolve():
    files = [ROOT / "README.md", ROOT / "docs" / "README.md", ROOT / "docs" / "EVALS.md", ROOT / "COST.md"]
    files += list((ROOT / "docs" / "CASES").glob("*.md"))
    broken = []
    for f in files:
        for m in re.finditer(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", f.read_text(encoding="utf-8")):
            t = m.group(1)
            if t.startswith(("http", "mailto:")):
                continue
            if not (f.parent / t).resolve().exists():
                broken.append(f"{f.name} -> {t}")
    assert not broken, broken


def test_no_issue_placeholder_left():
    for f in [ROOT / "README.md", ROOT / "COST.md", ROOT / "docs" / "EVALS.md", *CASES]:
        assert "#TBD" not in f.read_text(encoding="utf-8"), f.name


def test_case_0006_quotes_the_committed_baselines():
    doc = (ROOT / "docs" / "CASES" / "0006-golden-set-and-geo-guard.md").read_text(encoding="utf-8")
    g = json.loads((ROOT / "evals" / "results" / "golden_report_2026-11-09.json").read_text(encoding="utf-8"))
    geo = json.loads((ROOT / "evals" / "results" / "geo_guard_2026-11-09.json").read_text(encoding="utf-8"))

    # headline numbers (summary) and the threshold table
    t60 = g["main"]["T60"]
    assert f"**Precision@60 ≈ {t60['precision']:.3f}** (95 %: {_rng(*t60['precision_95'])})" in doc
    assert f"**recall@60 ≈ {t60['recall']:.3f}** ({_rng(*t60['recall_95'])})" in doc
    t50 = g["main"]["T50"]
    assert (f"precision {t50['precision']:.3f} ({_rng(*t50['precision_95'])}) and recall\n"
            f"  {t50['recall']:.3f} ({_rng(*t50['recall_95'])})") in doc
    for t in ("T50", "T55", "T60", "T65", "T70"):
        v = g["main"][t]
        row = (f"| {t[1:]} | {v['precision']:.3f} ({_rng(*v['precision_95'])}) | "
               f"{v['recall']:.3f} ({_rng(*v['recall_95'])}) |")
        assert row in doc, row
    s = g["sensitivity_geo_stale_pay_other_count_as_irrelevant"]
    assert f"precision@60 **{s['T60']['precision']:.3f}** ({_rng(*s['T60']['precision_95'])})" in doc
    assert f"@50 {s['T50']['precision']:.3f}" in doc

    # label noise and control pairs
    n = g["label_noise_precision_recall_at_60"]
    assert f"lifts recall from\n{n['flip_0pct']['recall']:.3f} to {n['flip_15pct']['recall']:.3f}" in doc
    c = g["label_noise_control_pairs"]
    assert f"κ = {c['kappa_3class']:.2f}" in doc and f"{_pct(c['agreement_3class'])} % (3 classes)" in doc
    assert f"κ = {c['kappa_binary']:.2f}" in doc

    # calibration table
    for band, label in (("<25", "< 25"), ("25-39", "25–39"), ("40-49", "40–49"), ("50-59", "50–59"),
                        ("60-69", "60–69"), ("70-79", "70–79"), ("80+", "80+")):
        v = g["calibration_by_current_score"][band]
        lo, hi = v["wilson_95"]
        share = f"{_pct(v['share'])} %"
        assert f"| {label} | {v['decided']} | {v['relevant']} |" in doc, band
        line = next(l for l in doc.splitlines() if l.startswith(f"| {label} |"))
        assert share in line.replace("**", ""), (band, line)
        if v["decided"] >= 3 or band in ("60-69",):
            assert f"({_pct(lo)}–{_pct(hi)} %)" in line, (band, line)

    # n, classes, frame sizes, the >= 60 rejections
    cl = g["classes"]
    assert (f"{g['labelled_core_vacancies']} labelled core vacancies: {cl['relevant']} relevant, "
            f"{cl['irrelevant']} irrelevant, {cl['excluded']} excluded, {cl['other']} other, {cl['unsure']} unsure") in doc
    fs = g["frame_band_sizes"]
    def sp(n): return f"{n:,}".replace(",", " ")
    for band, label in (("<25", "<25"), ("25-39", "25–39"), ("40-49", "40–49"), ("50-59", "50–59"),
                        ("60-69", "60–69"), ("70-79", "70–79"), ("80+", "80+")):
        assert f"`{label}` {sp(fs[band])}" in doc, band
    r = g["reasons_score_60_plus"]["by_label_or_chips"]
    rejected = g["reasons_score_60_plus"]["cards"] - r["yes"]
    only_geo_stale = r["geo"] + r["stale"] + r["geo+stale"]
    assert f"Of {rejected} rejected cards at score ≥ 60, **{only_geo_stale} were rejected only" in doc

    # geo guard table
    d, h = geo["dev"], geo["held_out"]
    for name, blk in (("dev (as labelled)", d["as_labelled"]), ("held-out (as labelled)", h["as_labelled"])):
        gc, y = blk["geo_chip"], blk["yes"]
        row = f"| {name} | {d['vacancies'] if name.startswith('dev') else h['vacancies']} | "
        row += f"{gc['cut']} / {gc['unknown']} / {gc['eligible']} of {gc['n']} | {y['wrongly_cut']} of {y['n']} |"
        assert row in doc, row
    for name, blk, key in (("dev (without 3 acknowledged label errors)", d, "dev"),
                           ("held-out (without 1 acknowledged label error)", h, "held")):
        w = blk["without_acknowledged_label_errors"]
        gc, y = w["geo_chip"], w["yes"]
        line = next(l for l in doc.splitlines() if l.startswith(f"| {name} |")).replace("**", "")
        assert f"{gc['cut']} / {gc['unknown']} / {gc['eligible']} of {gc['n']}" in line
        assert f"{y['wrongly_cut']} of {y['n']}" in line
        assert w["excluded"] == (3 if key == "dev" else 1)
