"""Scan configurable roots for git work trees."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
    }
)


@dataclass
class RepoEntry:
    path: str
    name: str
    branch: str
    dirty: bool
    deprecated: bool


def _is_git_work_tree(path: Path) -> bool:
    git = path / ".git"
    return git.is_dir() or git.is_file()


def find_repos(
    roots: list[Path],
    *,
    include_deprecated: bool = False,
    max_depth: int = 5,
) -> list[RepoEntry]:
    found: list[RepoEntry] = []
    seen: set[Path] = set()
    for root in roots:
        root = root.expanduser()
        if not root.exists():
            continue
        for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            try:
                rel_depth = len(current.relative_to(root).parts)
            except ValueError:
                dirnames[:] = []
                continue
            if rel_depth > max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [
                d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith(".git")
            ]
            deprecated = "DEPRECATED" in current.parts
            if deprecated and not include_deprecated:
                dirnames[:] = []
                continue
            if not _is_git_work_tree(current):
                continue
            resolved = current.resolve()
            if resolved in seen:
                dirnames[:] = []
                continue
            seen.add(resolved)
            branch, dirty = _quick_status(current)
            found.append(
                RepoEntry(
                    path=str(resolved),
                    name=current.name,
                    branch=branch,
                    dirty=dirty,
                    deprecated=deprecated,
                )
            )
            dirnames[:] = []
    found.sort(key=lambda r: r.path.lower())
    return found


def _quick_status(repo: Path) -> tuple[str, bool]:
    from .gitops import changed_paths, git
    from .models import SafetyError
    from .safety import is_junk, is_meta_path

    try:
        # symbolic-ref also works before a repository's first commit.
        branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        name = branch.stdout.strip() if branch.returncode == 0 else "HEAD"
        dirty = any(
            not is_meta_path(path) and not is_junk(path)
            for path in changed_paths(repo)
        )
        return name or "?", dirty
    except (OSError, SafetyError):
        # An unreadable index must not be presented as a clean repository.
        return "?", True
