"""Git operations on the target work tree. Never force-push, never amend, never delete branches."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

from .models import SafetyError, normalize_rel
from .safety import PROTECTED_BRANCHES

NIGHTSHIFT_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Nightshift",
    "GIT_AUTHOR_EMAIL": "nightshift@localhost",
    "GIT_COMMITTER_NAME": "Nightshift",
    "GIT_COMMITTER_EMAIL": "nightshift@localhost",
}


def git(
    repo: Path,
    *args: str,
    check: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if check and proc.returncode != 0:
        raise SafetyError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    return proc


def current_branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def rev_parse(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", ref).stdout.strip()


def default_branch(repo: Path) -> str:
    for name in ("main", "master"):
        proc = git(repo, "rev-parse", "--verify", name, check=False)
        if proc.returncode == 0:
            return name
    return current_branch(repo)


def list_local_branches(repo: Path) -> set[str]:
    proc = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def night_branch_name(existing: set[str], now: datetime) -> str:
    base = f"night/{now.strftime('%Y-%m-%d')}"
    if base not in existing:
        return base
    tagged = f"{base}-{now.strftime('%H%M')}"
    if tagged not in existing:
        return tagged
    tagged_s = f"{base}-{now.strftime('%H%M%S')}"
    if tagged_s not in existing:
        return tagged_s
    i = 2
    while f"{tagged_s}-{i}" in existing:
        i += 1
    return f"{tagged_s}-{i}"


def checkout_night_branch(repo: Path, now: datetime) -> str:
    name = night_branch_name(list_local_branches(repo), now)
    git(repo, "checkout", "-b", name)
    if current_branch(repo) in PROTECTED_BRANCHES:
        raise SafetyError("failed to leave main/master before the night")
    return name


def assert_not_protected(repo: Path) -> str:
    branch = current_branch(repo)
    if branch in PROTECTED_BRANCHES:
        raise SafetyError(f"refusing to commit to {branch}")
    return branch


def changed_paths(repo: Path) -> list[str]:
    porcelain = git(repo, "status", "--porcelain").stdout.splitlines()
    out: list[str] = []
    for line in porcelain:
        if not line.strip():
            continue
        raw = line[3:]
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        out.append(raw.strip().strip('"'))
    return out


def commit_paths(repo: Path, message: str, paths: list[str] | None = None) -> str | None:
    assert_not_protected(repo)
    if paths:
        existing = [p for p in paths if p]
        if not existing:
            return None
        # -f: .nightshift/ is often gitignored; freeze/summary still must land
        git(repo, "add", "-f", "--", *existing)
    else:
        git(repo, "add", "-A", "--", ".")
    staged = git(repo, "diff", "--cached", "--name-only").stdout.strip()
    if not staged:
        git(repo, "reset", "-q", check=False)
        return None
    git(repo, "commit", "-m", message, extra_env=NIGHTSHIFT_GIT_ENV)
    return rev_parse(repo, "HEAD")


def revert_paths(repo: Path, paths: list[str]) -> list[str]:
    """Restore unapproved paths from HEAD.

    Skip and record any path that resolves outside the target repo via ../
    or absolute traversal instead of unlinking it. New files that stay
    inside the repo are unlinked (not git-clean -fd).
    """
    done: list[str] = []
    repo_resolved = repo.resolve()
    for rel in paths:
        if Path(rel).is_absolute():
            done.append(f"SKIP (outside repo): {rel}")
            continue
        rel_n = normalize_rel(rel)
        if not rel_n or rel_n.startswith(".nightshift/"):
            continue
        candidate = (repo / rel_n).resolve()
        try:
            candidate.relative_to(repo_resolved)
        except ValueError:
            done.append(f"SKIP (outside repo): {rel_n}")
            continue
        tracked = git(repo, "ls-files", "--", rel_n, check=False)
        if tracked.stdout.strip():
            git(repo, "checkout", "--", rel_n)
            done.append(rel_n)
        elif candidate.exists():
            if candidate.is_file() or candidate.is_symlink():
                candidate.unlink()
                done.append(rel_n)
            # never rmtree directories blindly
    return done


def log_oneline(repo: Path, n: int = 12) -> str:
    proc = git(repo, "log", f"-{n}", "--oneline", check=False)
    return proc.stdout.strip()


def diff_stat_against(repo: Path, base: str) -> str:
    proc = git(repo, "diff", "--stat", f"{base}...HEAD", check=False)
    return proc.stdout.strip()


def commits_since(repo: Path, base: str) -> str:
    proc = git(repo, "log", "--oneline", f"{base}..HEAD", check=False)
    return proc.stdout.strip()


def working_tree_diff(repo: Path) -> str:
    proc = git(repo, "diff", "HEAD", check=False)
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", check=False)
    extra = ""
    if untracked.stdout.strip():
        extra = "\n# untracked\n" + untracked.stdout
    return (proc.stdout + extra)[-20_000:]


def push_branch(repo: Path, branch: str) -> str:
    # never --force, never --delete
    proc = git(repo, "push", "-u", "origin", branch, check=False)
    if proc.returncode != 0:
        raise SafetyError(f"git push failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout + proc.stderr
