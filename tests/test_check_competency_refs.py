"""scripts/check_competency_refs.py: report-only checker of competency source_ref pointers."""
from __future__ import annotations

import importlib.util
import os

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "check_competency_refs.py")
_spec = importlib.util.spec_from_file_location("check_competency_refs", SCRIPT)
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)


def _repo(tmp_path):
    root = tmp_path / "demo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("import os\n\n\ndef alpha():\n    pass\n\n\nclass Beta:\n    pass\n")
    return root


def _status(data, tmp_path):
    return [(r[3], r[4]) for r in chk.check_all(data, {"COMPETENCY_REPO_DEMO": str(tmp_path / "demo")})]


def _data(ref):
    return {"core": [{"term_en": "T", "evidence": [{"project": "demo", "source_ref": ref}]}]}


def test_parse_ref_paths_lines_and_symbols():
    parsed = chk.parse_ref("a/b.py:12 (alpha), 40 (Beta.run); c/d.py (note); e/*.py, f/g.py:3 (h)")
    assert parsed[0] == ("a/b.py", [(12, "alpha"), (40, "run")])
    assert parsed[1] == ("c/d.py", [(None, "note")])
    assert [p for p, _ in parsed[2:]] == ["e/*.py", "f/g.py"]
    assert parsed[3][1] == [(3, "h")]


def test_ok_drift_and_missing_symbol(tmp_path):
    _repo(tmp_path)
    assert _status(_data("pkg/mod.py:4 (alpha)"), tmp_path)[0][0] == "OK"
    status, detail = _status(_data("pkg/mod.py:99 (alpha)"), tmp_path)[0]
    assert status == "DRIFT" and "now line 4" in detail
    assert _status(_data("pkg/mod.py:4 (gamma)"), tmp_path)[0][0] == "MISSING_SYMBOL"


def test_missing_file_glob_and_path_only(tmp_path):
    _repo(tmp_path)
    assert _status(_data("pkg/nope.py:1 (alpha)"), tmp_path)[0][0] == "MISSING_FILE"
    assert _status(_data("pkg/*.py"), tmp_path)[0][0] == "NO_FILE"


def test_no_public_code_is_reported_honestly_not_as_unchecked(tmp_path):
    ref = "n/a, work at employer / NDA, no public code"
    assert _status(_data(ref), tmp_path)[0][0] == "NO_PUBLIC_CODE"


def test_missing_repo_is_a_missing_file_row(tmp_path):
    data = {"core": [{"term_en": "T", "evidence": [{"project": "ghost", "source_ref": "x.py:1 (y)"}]}]}
    rows = chk.check_all(data, {"COMPETENCY_REPO_GHOST": str(tmp_path / "ghost")})
    assert rows[0][3] == "MISSING_FILE"


def test_repo_root_env_variable_naming():
    assert chk.repo_root("nexus-arcana", {"COMPETENCY_REPO_NEXUS_ARCANA": "/x"}) == "/x"
    assert chk.repo_root("my_project", {}).endswith("/my_project")


def test_report_only_never_modifies_the_file(tmp_path):
    _repo(tmp_path)
    f = tmp_path / "c.yaml"
    f.write_text("core:\n  - term_en: T\n    evidence:\n      - project: demo\n        source_ref: pkg/mod.py:99 (alpha)\n")
    before = f.read_text()
    os.environ["COMPETENCY_REPO_DEMO"] = str(tmp_path / "demo")
    try:
        assert chk.main(["--file", str(f)]) == 0
        assert chk.main(["--file", str(f), "--strict"]) == 1
    finally:
        del os.environ["COMPETENCY_REPO_DEMO"]
    assert f.read_text() == before
