from __future__ import annotations

from pathlib import Path

from nightshift.host import argv_for, run_check
from nightshift.models import Upgrade


def test_argv_strips_ini_addopts_for_pytest(tmp_path: Path):
    argv = argv_for("python -m pytest tests/test_x.py -q", tmp_path)
    assert "-o" in argv
    assert "addopts=" in argv
    assert argv[argv.index("-o") + 1] == "addopts="


def test_host_pytest_survives_cov_addopts(tmp_path: Path):
    repo = tmp_path / "proj"
    (repo / "tests").mkdir(parents=True)
    (repo / "pytest.ini").write_text(
        "[pytest]\naddopts =\n    --cov=src\n    --cov-report=term-missing\n"
    )
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    row = run_check(
        repo,
        Upgrade(1, "ok", "python -m pytest tests/test_ok.py -q", ["tests/test_ok.py"]),
        timeout=30,
    )
    assert row.ok, row.output
    assert "unrecognized arguments" not in (row.output or "")


def test_needs_shell_for_bang_grep():
    from nightshift.host import needs_shell

    assert needs_shell("! grep -q foo README.md && grep -q bar README.md")
    assert not needs_shell("python -m pytest tests/test_ok.py -q")


def test_host_bang_grep_runs_in_shell(tmp_path: Path):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n")
    row = run_check(
        repo,
        Upgrade(
            1,
            "no foo",
            "! grep -q foo README.md && grep -q hello README.md",
            ["README.md"],
        ),
        timeout=10,
    )
    assert row.ok, row.output
    assert "No such file or directory" not in (row.output or "")
