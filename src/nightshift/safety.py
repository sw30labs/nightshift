"""Hard limits: this is personal-capacity OSS, never employer-shaped."""

from __future__ import annotations

from pathlib import Path

from .models import SafetyError, normalize_rel

PROTECTED_BRANCHES = frozenset({"main", "master"})
BLOCKED_WRITE_NAMES = frozenset(
    {".env", ".env.local", "id_rsa", "id_ed25519", "credentials.json"}
)


def resolve_repo(path: Path) -> Path:
    return path.expanduser().resolve()


def is_nightshift_repo(path: Path) -> bool:
    root = resolve_repo(path)
    marker = root / "src" / "nightshift" / "cli.py"
    pyproject = root / "pyproject.toml"
    if not marker.is_file() or not pyproject.is_file():
        return False
    try:
        text = pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return 'name = "nightshift"' in text or "name = 'nightshift'" in text


def assert_safe_target(path: Path, *, explicit: bool) -> Path:
    root = resolve_repo(path)
    if root == Path("/") or root == Path.home().resolve():
        raise SafetyError("refusing to run against / or $HOME")
    if is_nightshift_repo(root) and not explicit:
        raise SafetyError(
            "refusing to run against Nightshift's own repo unless explicitly selected"
        )
    git_dir = root / ".git"
    if not git_dir.exists():
        raise SafetyError(f"not a git work tree: {root}")
    return root


def assert_inside_repo(repo: Path, rel: str) -> Path:
    repo_r = resolve_repo(repo)
    candidate = (repo_r / rel).resolve()
    try:
        candidate.relative_to(repo_r)
    except ValueError as exc:
        raise SafetyError(f"path escapes the target repo: {rel}") from exc
    parts = set(candidate.relative_to(repo_r).parts)
    if ".git" in parts:
        raise SafetyError("writer may not touch .git/")
    if candidate.name in BLOCKED_WRITE_NAMES:
        raise SafetyError(f"refusing to write {candidate.name}")
    return candidate


def is_meta_path(rel: str) -> bool:
    return normalize_rel(rel).startswith(".nightshift/")
