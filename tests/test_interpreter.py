from __future__ import annotations

import os
from pathlib import Path

from nightshift.host import _INTERP_CACHE, argv_for, resolve_interpreter, run_check
from nightshift.models import Upgrade


def test_interpreter_precedence(tmp_path, monkeypatch):
    _INTERP_CACHE.clear()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))

    envs = home / ".conda"
    envs.mkdir()
    env_twin = tmp_path / "envs" / "nsuniq-twin"
    env_norm = tmp_path / "envs" / "screenlens"
    for prefix in (env_twin, env_norm):
        (prefix / "bin").mkdir(parents=True)
        py = prefix / "bin" / "python"
        py.write_text("#!/bin/sh\n")
        py.chmod(0o755)

    def fake_prefixes():
        return [env_twin, env_norm]

    monkeypatch.setattr("nightshift.host._conda_prefixes", fake_prefixes)

    repo = tmp_path / "nsuniq-twin"
    repo.mkdir()
    (repo / "environment.yml").write_text("name: othername\n")
    (repo / ".venv" / "bin").mkdir(parents=True)
    venv_py = repo / ".venv" / "bin" / "python"
    venv_py.write_text("#!/bin/sh\n")
    venv_py.chmod(0o755)

    # .venv wins over yml/conda
    _INTERP_CACHE.clear()
    picked = resolve_interpreter(repo)
    assert picked.source == ".venv"
    assert picked.path == str(venv_py)

    # override wins
    monkeypatch.setenv("NIGHTSHIFT_TARGET_PYTHON", "/opt/custom/bin/python")
    _INTERP_CACHE.clear()
    picked = resolve_interpreter(repo)
    assert picked.source == "override"
    assert picked.path == "/opt/custom/bin/python"
    monkeypatch.delenv("NIGHTSHIFT_TARGET_PYTHON")

    # host.json override
    _INTERP_CACHE.clear()
    (repo / ".nightshift").mkdir()
    (repo / ".nightshift" / "host.json").write_text('{"python": "/opt/hostjson/python"}\n')
    picked = resolve_interpreter(repo)
    assert picked.source == "override"
    (repo / ".nightshift" / "host.json").unlink()

    # no venv, yml name missing → conda dir name
    (repo / ".venv" / "bin" / "python").unlink()
    _INTERP_CACHE.clear()
    picked = resolve_interpreter(repo)
    # yml name is othername, not in conda list → dir name twinops
    assert picked.source == "conda env: dir name"
    assert str(env_twin / "bin" / "python") == picked.path

    # normalized name must not match
    other = tmp_path / "screen-lens"
    other.mkdir()
    _INTERP_CACHE.clear()
    picked = resolve_interpreter(other)
    assert picked.source == "nightshift-env fallback"


def test_argv_and_path_for_pytest(tmp_path, monkeypatch):
    _INTERP_CACHE.clear()
    repo = tmp_path / "proj"
    (repo / ".venv" / "bin").mkdir(parents=True)
    py = repo / ".venv" / "bin" / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    argv = argv_for("pytest -q", repo)
    assert argv[0] == str(py)
    assert argv[1:3] == ["-m", "pytest"]
    assert "-o" in argv and "addopts=" in argv

    captured = {}

    def fake_popen(*a, **k):
        captured["env"] = k.get("env")

        class Proc:
            returncode = 0

            def communicate(self, timeout=None):
                return ("ok\n", "")

        return Proc()

    monkeypatch.setattr("nightshift.host.subprocess.Popen", fake_popen)
    run_check(repo, Upgrade(1, "t", "true", ["a.py"]), timeout=5)
    path = captured["env"]["PATH"]
    assert path.split(os.pathsep)[0] == str(repo / ".venv" / "bin")
