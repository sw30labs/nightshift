"""Host checks. Pytest/output is truth, not the model's opinion."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import CheckResult, Upgrade

_INTERP_CACHE: dict[str, "Interpreter"] = {}

_PYTEST_NODE = re.compile(
    r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)"
)
_FAILED_COUNT = re.compile(r"(\d+)\s+failed")
_ERROR_COUNT = re.compile(r"(\d+)\s+error")
_EXC_TYPE = re.compile(r"- (\w+(?:Error|Exception|Failed))\b")
_CHECK_FILE_RE = re.compile(
    r"[\w./-]+\.(?:py|js|ts|md|toml|ini|cfg|ya?ml|txt|json|sh)"
)


@dataclass
class Interpreter:
    path: str
    source: str


def _python_at(prefix: Path) -> Path | None:
    cand = prefix / "bin" / "python"
    return cand if cand.is_file() else None


def _conda_prefixes() -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()

    def _add(prefix: Path) -> None:
        try:
            resolved = prefix.expanduser().resolve()
        except OSError:
            return
        if resolved in seen:
            return
        if _python_at(resolved) is None:
            return
        seen.add(resolved)
        found.append(resolved)

    env_txt = Path.home() / ".conda" / "environments.txt"
    try:
        if env_txt.is_file():
            for line in env_txt.read_text(encoding="utf-8", errors="replace").splitlines():
                raw = line.strip()
                if raw and not raw.startswith("#"):
                    _add(Path(raw))
    except OSError:
        pass
    for extra in (os.environ.get("CONDA_ENVS_PATH") or "").split(os.pathsep):
        extra = extra.strip()
        if not extra:
            continue
        root = Path(extra)
        if not root.is_dir():
            continue
        try:
            for child in root.iterdir():
                if child.is_dir():
                    _add(child)
        except OSError:
            pass
    conda_prefix = os.environ.get("CONDA_PREFIX") or ""
    if conda_prefix:
        root = Path(conda_prefix)
        _add(root)
        parent_envs = root.parent
        if parent_envs.name == "envs" and parent_envs.is_dir():
            try:
                for child in parent_envs.iterdir():
                    if child.is_dir():
                        _add(child)
            except OSError:
                pass
    # CONDA_ROOT/envs plus typical install locations
    for root_s in (
        os.environ.get("CONDA_ROOT"),
        os.environ.get("MAMBA_ROOT_PREFIX"),
        str(Path.home() / "miniconda3"),
        str(Path.home() / "miniforge3"),
        str(Path.home() / "anaconda3"),
        str(Path.home() / "mambaforge"),
    ):
        if not root_s:
            continue
        envs = Path(root_s) / "envs"
        if not envs.is_dir():
            continue
        try:
            for child in envs.iterdir():
                if child.is_dir():
                    _add(child)
        except OSError:
            continue
    return found


def _yml_env_name(repo: Path) -> str | None:
    for name in ("environment.yml", "environment.yaml"):
        path = repo / name
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip()
                if stripped.startswith("name:"):
                    return stripped.split(":", 1)[1].strip().strip("'\"")
        except OSError:
            continue
    return None


def resolve_interpreter(repo: Path | None) -> Interpreter:
    if repo is None:
        return Interpreter(path=sys.executable, source="nightshift-env fallback")
    try:
        key = str(repo.expanduser().resolve())
    except OSError:
        key = str(repo)
    env_override = (os.environ.get("NIGHTSHIFT_TARGET_PYTHON") or "").strip()
    if env_override:
        picked = Interpreter(path=env_override, source="override")
        _INTERP_CACHE[key] = picked
        return picked
    host_json = repo / ".nightshift" / "host.json"
    if host_json.is_file():
        try:
            data = json.loads(host_json.read_text(encoding="utf-8"))
            py = str(data.get("python") or "").strip() if isinstance(data, dict) else ""
            if py:
                picked = Interpreter(path=py, source="override")
                _INTERP_CACHE[key] = picked
                return picked
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    cached = _INTERP_CACHE.get(key)
    if cached is not None and cached.source != "override" and Path(cached.path).is_file():
        return cached
    for rel in (".venv/bin/python", "venv/bin/python"):
        cand = repo / rel
        if cand.is_file():
            picked = Interpreter(path=str(cand), source=".venv")
            _INTERP_CACHE[key] = picked
            return picked
    prefixes = _conda_prefixes()
    by_name = {p.name: p for p in prefixes}
    yml_name = _yml_env_name(repo)
    if yml_name and yml_name in by_name:
        py = _python_at(by_name[yml_name])
        if py is not None:
            picked = Interpreter(path=str(py), source="environment.yml")
            _INTERP_CACHE[key] = picked
            return picked
    dir_name = repo.name
    if dir_name in by_name:
        py = _python_at(by_name[dir_name])
        if py is not None:
            picked = Interpreter(path=str(py), source="conda env: dir name")
            _INTERP_CACHE[key] = picked
            return picked
    picked = Interpreter(path=sys.executable, source="nightshift-env fallback")
    _INTERP_CACHE[key] = picked
    return picked


def interpreter_for(repo: Path | None) -> str:
    return resolve_interpreter(repo).path


def _is_python_exe(exe: str) -> bool:
    name = Path(exe).name.lower()
    return name in {"python", "python3", "py"} or name.startswith("python")


def needs_shell(command: str) -> bool:
    """Detect shell syntax without interpreting punctuation inside code strings."""
    quote = ""
    escaped = False
    for ch in command:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = ""
            elif quote == '"' and ch in "$`":
                return True
            continue
        if ch in "'\"":
            quote = ch
        elif ch in ";|&`!><*?~$\n()":
            return True
    # VAR=value cmd
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=\S+\s+\S", command.strip()):
        return True
    return False


def _inject_pytest_flags(parts: list[str]) -> list[str]:
    pytest_idx: int | None = None
    for i, tok in enumerate(parts):
        name = Path(tok).name
        if name == "pytest" or tok == "pytest":
            pytest_idx = i
            break
        if tok == "-m" and i + 1 < len(parts) and parts[i + 1] == "pytest":
            pytest_idx = i + 1
            break
    if pytest_idx is None:
        return parts
    if "addopts=" not in parts:
        parts.insert(pytest_idx + 1, "-o")
        parts.insert(pytest_idx + 2, "addopts=")
        insert_at = pytest_idx + 3
    else:
        insert_at = parts.index("addopts=") + 1
    if "-rA" not in parts:
        parts.insert(insert_at, "-rA")
    return parts


def argv_for(command: str, repo: Path | None = None) -> list[str]:
    parts = shlex.split(command, posix=True)
    if not parts:
        raise ValueError("empty check command")
    py = interpreter_for(repo)
    if parts[0] == "pytest" or Path(parts[0]).name == "pytest":
        parts = [py, "-m", "pytest", *parts[1:]]
    elif _is_python_exe(parts[0]):
        parts[0] = py
    return _inject_pytest_flags(parts)


_SHELL_PYTHON = re.compile(
    r"(?:^|[;&|(\n])\s*(?:!\s+)?"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;&|()]+\s+)*"
    r"(?P<exe>python(?:\d+(?:\.\d+)*)?|pytest)(?=\s|$|[;&|)])"
)


def rewrite_shell_command(command: str, repo: Path | None = None) -> str:
    quoted = shlex.quote(interpreter_for(repo))
    # Match executable tokens only, retaining every original quoted string.
    # Regex replacement across raw shell text used to rewrite Python source.
    masked = list(command)
    quote = ""
    escaped = False
    for i, ch in enumerate(command):
        if escaped:
            masked[i] = " "
            escaped = False
        elif ch == "\\" and quote != "'":
            masked[i] = " "
            escaped = True
        elif quote:
            masked[i] = " "
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            masked[i] = " "
            quote = ch
    plain = "".join(masked)
    replacements: list[tuple[int, int, str]] = []
    for match in _SHELL_PYTHON.finditer(plain):
        start, end = match.span("exe")
        exe = match.group("exe")
        if exe == "pytest":
            replacement = f"{quoted} -m pytest -o addopts= -rA"
        else:
            replacement = quoted
            module = re.match(r"\s+-m\s+pytest(?=\s|$|[;&|)])", plain[end:])
            if module:
                end += module.end()
                replacement += " -m pytest -o addopts= -rA"
        replacements.append((start, end, replacement))
    for start, end, replacement in reversed(replacements):
        command = command[:start] + replacement + command[end:]
    return command


def parse_pytest(output: str) -> dict[str, Any]:
    passed: set[str] = set()
    failed: set[str] = set()
    errors: set[str] = set()
    counts = ""
    collection_error = False
    for line in (output or "").splitlines():
        stripped = line.strip()
        m = _PYTEST_NODE.match(stripped)
        if m:
            kind, node = m.group(1), m.group(2)
            if kind == "PASSED" or kind == "XPASS":
                passed.add(node)
            elif kind == "FAILED":
                failed.add(node)
            elif kind == "ERROR":
                errors.add(node)
            continue
        if stripped.startswith("=") and stripped.endswith("="):
            inner = stripped.strip("= ")
            if inner:
                counts = inner
            low = inner.lower()
            if "error" in low and "collected" in (output or "").lower():
                pass
        if "ERROR collecting" in line or "Interrupted: " in line:
            collection_error = True
    if not counts:
        for line in reversed((output or "").splitlines()):
            s = line.strip()
            if s.startswith("=") and s.endswith("=") and len(s) > 4:
                counts = s.strip("= ")
                break
    if "ERROR collecting" in (output or "") or "not found: " in (output or "").lower():
        collection_error = True
    return {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "counts": counts,
        "collection_error": collection_error,
    }


def count_failed(output: str) -> int:
    parsed = parse_pytest(output)
    n = len(parsed["failed"]) + len(parsed["errors"])
    if n:
        return n
    failed = _FAILED_COUNT.findall(output or "")
    errors = _ERROR_COUNT.findall(output or "")
    return (int(failed[-1]) if failed else 0) + (int(errors[-1]) if errors else 0)


def check_command_file_tokens(command: str) -> list[str]:
    return _CHECK_FILE_RE.findall(command or "")


def _clip_output(text: str, head: int = 3000, tail: int = 5000) -> str:
    if len(text) <= head + tail + 10:
        return text
    return text[:head] + "\n…\n" + text[-tail:]


def _check_env(repo: Path, interp: Interpreter) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if interp.source in {".venv", "environment.yml", "conda env: dir name"}:
        bindir = str(Path(interp.path).parent)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = str(Path(interp.path).parent.parent)
        if interp.source != ".venv":
            env["CONDA_PREFIX"] = str(Path(interp.path).parent.parent)
    return env


def _kill_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def run_check(repo: Path, upgrade: Upgrade, timeout: int) -> CheckResult:
    command = upgrade.check_command
    try:
        interp = resolve_interpreter(repo)
        env = _check_env(repo, interp)
        if needs_shell(command):
            rewritten = rewrite_shell_command(command, repo)
            shell = shutil.which("bash")
            if shell is None:
                raise ValueError("bash is required for compound host checks with pipefail")
            popen: str | list[str] = [shell, "-o", "pipefail", "-c", rewritten]
        else:
            popen = argv_for(command, repo)
    except (OSError, ValueError, RuntimeError) as exc:
        return CheckResult(
            upgrade_id=upgrade.id,
            command=upgrade.check_command,
            ok=False,
            exit_code=-1,
            output=str(exc),
        )
    try:
        proc = subprocess.Popen(
            popen,
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            env=env,
            start_new_session=True,
        )
        try:
            out, _err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                out, _err = proc.communicate(timeout=2)
            except Exception:
                out = ""
            return CheckResult(
                upgrade_id=upgrade.id,
                command=upgrade.check_command,
                ok=False,
                exit_code=-1,
                output=_clip_output(f"timeout after {timeout}s\n{out or ''}"),
            )
        output = _clip_output(out or "")
        return CheckResult(
            upgrade_id=upgrade.id,
            command=upgrade.check_command,
            ok=proc.returncode == 0,
            exit_code=int(proc.returncode or 0),
            output=output,
        )
    except OSError as exc:
        return CheckResult(
            upgrade_id=upgrade.id,
            command=upgrade.check_command,
            ok=False,
            exit_code=-1,
            output=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — host check must never kill the night
        return CheckResult(
            upgrade_id=upgrade.id,
            command=upgrade.check_command,
            ok=False,
            exit_code=-1,
            output=str(exc),
        )


def run_remaining(repo: Path, upgrades: list[Upgrade], timeout: int) -> list[CheckResult]:
    return [run_check(repo, u, timeout) for u in upgrades]
