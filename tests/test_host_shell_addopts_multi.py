from __future__ import annotations

from pathlib import Path

from nightshift.host import rewrite_shell_command, run_check
from nightshift.models import Upgrade


def _count(text: str, needle: str) -> int:
    return text.count(needle)


def test_two_python_m_pytest_invocations_both_get_addopts(tmp_path: Path):
    cmd = "python -m pytest tests/a.py -q && python -m pytest tests/b.py -q"
    out = rewrite_shell_command(cmd, tmp_path)
    assert _count(out, "-o addopts=") == 2
    assert _count(out, "-rA") == 2
    assert "tests/a.py" in out and "tests/b.py" in out


def test_two_bare_pytest_invocations_both_get_addopts(tmp_path: Path):
    cmd = "pytest tests/a.py -q && pytest tests/b.py -q"
    out = rewrite_shell_command(cmd, tmp_path)
    assert _count(out, "-o addopts=") == 2
    assert _count(out, "-rA") == 2


def test_mixed_python_and_bare_pytest_both_get_addopts(tmp_path: Path):
    cmd = "python -m pytest tests/a.py -q && pytest tests/b.py -q"
    out = rewrite_shell_command(cmd, tmp_path)
    assert _count(out, "-o addopts=") == 2
    assert _count(out, "-rA") == 2


def test_three_invocations_with_semicolons(tmp_path: Path):
    cmd = "python -m pytest tests/a.py -q; pytest tests/b.py -q; python -m pytest tests/c.py -q"
    out = rewrite_shell_command(cmd, tmp_path)
    assert _count(out, "-o addopts=") == 3
    assert _count(out, "-rA") == 3


def test_quoted_pytest_text_is_not_rewritten(tmp_path: Path):
    cmd = "python -m pytest tests/a.py -q && echo 'pytest tests/b.py -q'"
    out = rewrite_shell_command(cmd, tmp_path)
    assert _count(out, "-o addopts=") == 1
    assert "echo 'pytest tests/b.py -q'" in out


def test_shell_form_survives_cov_addopts_for_two_invocations(tmp_path: Path):
    repo = tmp_path / "proj"
    (repo / "tests").mkdir(parents=True)
    (repo / "pytest.ini").write_text(
        "[pytest]\naddopts =\n    --cov=src\n    --cov-report=term-missing\n"
    )
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    (repo / "tests" / "test_b.py").write_text("def test_b():\n    assert True\n")
    row = run_check(
        repo,
        Upgrade(
            1,
            "two shell pytest invocations",
            "python -m pytest tests/test_a.py -q && python -m pytest tests/test_b.py -q",
            ["tests/test_a.py", "tests/test_b.py"],
        ),
        timeout=30,
    )
    assert row.ok, row.output
    assert "unrecognized arguments" not in (row.output or "")
