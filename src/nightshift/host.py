"""Host checks. Pytest/output is truth, not the model's opinion."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from .models import CheckResult, Upgrade


def interpreter_for(repo: Path | None) -> str:
    if repo is not None:
        for rel in (".venv/bin/python", "venv/bin/python"):
            cand = repo / rel
            if cand.is_file():
                return str(cand)
    return sys.executable


def _is_python_exe(exe: str) -> bool:
    name = Path(exe).name.lower()
    return name in {"python", "python3", "py"} or name.startswith("python")


def needs_shell(command: str) -> bool:
    """Compound critic checks (!, &&, pipes) must run under /bin/sh."""
    return any(tok in command for tok in ("&&", "||", ";", "|", "`", "$(", "!", ">", "<"))


def argv_for(command: str, repo: Path | None = None) -> list[str]:
    parts = shlex.split(command, posix=True)
    if not parts:
        raise ValueError("empty check command")
    if _is_python_exe(parts[0]):
        parts[0] = interpreter_for(repo)
    if "pytest" in parts and "-o" not in parts:
        # Target pytest.ini often injects --cov. Nightshift's venv may not have
        # pytest-cov, and a single-file check should not inherit CI addopts.
        idx = parts.index("pytest")
        parts.insert(idx + 1, "-o")
        parts.insert(idx + 2, "addopts=")
    return parts


def run_check(repo: Path, upgrade: Upgrade, timeout: int) -> CheckResult:
    env = os.environ.copy()
    env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    command = upgrade.check_command
    if needs_shell(command):
        py = interpreter_for(repo)
        rewritten = command
        # Compound checks: strip CI addopts after each pytest token, not at EOL.
        for token in ("python3", "python"):
            rewritten = rewritten.replace(f"{token} -m pytest", f"{py} -m pytest -o addopts=")
            rewritten = rewritten.replace(f"{token} -c", f"{py} -c")
        rewritten = re.sub(
            r"(?<!\w)pytest(?!\s+-o)(?!\w)",
            "pytest -o addopts=",
            rewritten,
        )
        popen: str | list[str] = ["/bin/sh", "-c", rewritten]
    else:
        try:
            popen = argv_for(command, repo)
        except ValueError as exc:
            return CheckResult(
                upgrade_id=upgrade.id,
                command=upgrade.check_command,
                ok=False,
                exit_code=-1,
                output=str(exc),
            )
    try:
        proc = subprocess.run(
            popen,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        output = ((proc.stdout or "") + (proc.stderr or ""))[-8000:]
        return CheckResult(
            upgrade_id=upgrade.id,
            command=upgrade.check_command,
            ok=proc.returncode == 0,
            exit_code=proc.returncode,
            output=output,
        )
    except subprocess.TimeoutExpired as exc:
        tail = ""
        if exc.stdout:
            tail += exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode()
        if exc.stderr:
            tail += exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode()
        return CheckResult(
            upgrade_id=upgrade.id,
            command=upgrade.check_command,
            ok=False,
            exit_code=-1,
            output=f"timeout after {timeout}s\n{tail}"[-8000:],
        )
    except OSError as exc:
        return CheckResult(
            upgrade_id=upgrade.id,
            command=upgrade.check_command,
            ok=False,
            exit_code=-1,
            output=str(exc),
        )


def run_remaining(repo: Path, upgrades: list[Upgrade], timeout: int) -> list[CheckResult]:
    return [run_check(repo, u, timeout) for u in upgrades]
