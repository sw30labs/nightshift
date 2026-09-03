import subprocess
import tempfile
from pathlib import Path

import pytest

from nightshift.host import run_check
from nightshift.models import Upgrade


@pytest.fixture
def target_with_pytest_ini():
    """Create a temporary git repo with a pytest.ini that sets an unrecognized CI flag."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        # Init git repo
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo, check=True, capture_output=True)
        
        # Create a simple test file
        (repo / "test_foo.py").write_text(
            "def test_foo():\n    assert 1 + 1 == 2\n",
            encoding="utf-8",
        )
        
        # Create pytest.ini with an unrecognized CI flag (--cov)
        (repo / "pytest.ini").write_text(
            "[pytest]\naddopts = --cov\n",
            encoding="utf-8",
        )
        
        # Create a conftest.py so pytest doesn't complain about missing plugins
        (repo / "conftest.py").write_text("", encoding="utf-8")
        
        # Commit everything
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
        
        yield repo


def test_compound_bare_pytest_passes_with_pytest_ini(target_with_pytest_ini):
    """A compound check like 'pytest -q && echo ok' should pass even when
    the target's pytest.ini sets an unrecognized CI flag (--cov).
    The host.py shell path must strip addopts by inserting -o addopts=
    immediately after the pytest token."""
    repo = target_with_pytest_ini
    upgrade = Upgrade(
        id=1,
        title="Compound bare-pytest check with pytest.ini CI addopts",
        check_command="pytest -q && echo ok",
        paths=["test_foo.py"],
    )
    result = run_check(repo, upgrade, timeout=30)
    assert result.ok, (
        f"Compound check failed: exit={result.exit_code}\n{result.output}"
    )
    # The output should contain 'ok' from the echo
    assert "ok" in result.output, f"Expected 'ok' in output: {result.output}"


def test_compound_python_m_pytest_passes_with_pytest_ini(target_with_pytest_ini):
    """A compound check using 'python -m pytest' should also work."""
    repo = target_with_pytest_ini
    upgrade = Upgrade(
        id=2,
        title="Compound python -m pytest check with pytest.ini CI addopts",
        check_command="python -m pytest -q && echo ok",
        paths=["test_foo.py"],
    )
    result = run_check(repo, upgrade, timeout=30)
    assert result.ok, (
        f"Compound python -m pytest check failed: exit={result.exit_code}\n{result.output}"
    )
    assert "ok" in result.output, f"Expected 'ok' in output: {result.output}"


def test_simple_pytest_passes_with_pytest_ini(target_with_pytest_ini):
    """A simple 'pytest -q' check should also pass (regression)."""
    repo = target_with_pytest_ini
    upgrade = Upgrade(
        id=3,
        title="Simple bare-pytest check with pytest.ini CI addopts",
        check_command="pytest -q",
        paths=["test_foo.py"],
    )
    result = run_check(repo, upgrade, timeout=30)
    assert result.ok, (
        f"Simple check failed: exit={result.exit_code}\n{result.output}"
    )


def test_compound_with_semicolon_passes(target_with_pytest_ini):
    """A compound check using semicolons should also work."""
    repo = target_with_pytest_ini
    upgrade = Upgrade(
        id=4,
        title="Compound check with semicolon",
        check_command="pytest -q; echo ok",
        paths=["test_foo.py"],
    )
    result = run_check(repo, upgrade, timeout=30)
    assert result.ok, (
        f"Compound semicolon check failed: exit={result.exit_code}\n{result.output}"
    )
    assert "ok" in result.output, f"Expected 'ok' in output: {result.output}"
