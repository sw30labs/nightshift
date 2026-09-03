from __future__ import annotations

from nightshift.config import Settings
from nightshift.host import rewrite_shell_command
from nightshift.safety import assert_inside_repo
from nightshift.models import SafetyError
import pytest


def test_nightshift_port_env(monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_PORT", "43999")
    s = Settings.from_cli()
    assert s.deck_port == 43999
    s = Settings.from_cli(port=43171)
    assert s.deck_port == 43171


def test_shell_rewrite_does_not_eat_absolute_interpreter(tmp_path):
    py = "/opt/conda/envs/foo/bin/python"
    cmd = f"{py} -m pytest -q && echo ok"
    # rewrite looks for bare python, not a path ending in python
    out = rewrite_shell_command(cmd, None)
    assert out.count("-m pytest") >= 1
    # should not produce /opt/conda/envs/foo/bin/<quoted interpreter> doubled
    assert "bin/python -m pytest -o addopts=" in out or py in out


def test_git_dir_case_insensitive(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / ".git").mkdir()
    with pytest.raises(SafetyError, match=".git"):
        assert_inside_repo(repo, ".GIT/config")
