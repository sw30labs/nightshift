"""Git operations on the target work tree. Never force-push, never amend, never delete branches."""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from .models import SafetyError, normalize_rel
from .safety import PROTECTED_BRANCHES, is_blocked_rel, is_junk, is_meta_path

NIGHTSHIFT_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Nightshift",
    "GIT_AUTHOR_EMAIL": "nightshift@localhost",
    "GIT_COMMITTER_NAME": "Nightshift",
    "GIT_COMMITTER_EMAIL": "nightshift@localhost",
}

_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


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


def unescape_git_path(raw: str) -> str:
    """Undo core.quotepath C-style quoting (`caf\\303\\251.txt`)."""
    s = (raw or "").strip()
    quoted = len(s) >= 2 and s[0] == '"' and s[-1] == '"'
    if quoted:
        s = s[1:-1]
    if "\\" not in s:
        return s.replace("\\", "/")

    def _oct(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 8))

    s = _OCTAL_ESCAPE.sub(_oct, s)
    s = (
        s.replace(r"\\", "\\")
        .replace(r"\"", '"')
        .replace(r"\t", "\t")
        .replace(r"\n", "\n")
        .replace(r"\r", "\r")
    )
    return s.replace("\\", "/")


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


def checkout_night_branch(repo: Path, now: datetime, name: str | None = None) -> str:
    branch = name or night_branch_name(list_local_branches(repo), now)
    git(repo, "checkout", "-b", branch)
    if current_branch(repo) in PROTECTED_BRANCHES:
        raise SafetyError("failed to leave main/master before the night")
    return branch


def assert_not_protected(repo: Path) -> str:
    branch = current_branch(repo)
    if branch in PROTECTED_BRANCHES:
        raise SafetyError(f"refusing to commit to {branch}")
    return branch


def changed_paths(repo: Path) -> list[str]:
    porcelain = git(
        repo,
        "-c",
        "core.quotepath=false",
        "status",
        "--porcelain",
        "--untracked-files=all",
    ).stdout.splitlines()
    out: list[str] = []
    seen: set[str] = set()
    for line in porcelain:
        if not line.strip():
            continue
        raw = line[3:] if len(line) > 3 else line
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        rel = unescape_git_path(raw.strip())
        rel = normalize_rel(rel)
        if not rel or rel in seen:
            continue
        seen.add(rel)
        out.append(rel)
    return out


def commit_paths(
    repo: Path,
    message: str,
    paths: list[str] | None = None,
    *,
    exclude: set[str] | None = None,
) -> str | None:
    assert_not_protected(repo)
    skip = {normalize_rel(p) for p in (exclude or set()) if str(p).strip()}
    if paths:
        existing = [p for p in paths if p]
        if not existing:
            return None
        # -f: .nightshift/ is often gitignored; freeze/summary still must land
        git(repo, "add", "-f", "--", *existing)
    else:
        to_add = [
            p
            for p in changed_paths(repo)
            if p not in skip and not is_junk(p)
        ]
        if not to_add:
            git(repo, "reset", "-q", check=False)
            return None
        present = [p for p in to_add if (repo / p).exists()]
        missing = [p for p in to_add if p not in present]
        for i in range(0, len(present), 500):
            git(repo, "add", "--", *present[i : i + 500])
        for i in range(0, len(missing), 500):
            git(repo, "add", "-u", "--", *missing[i : i + 500], check=False)
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
    inside the repo are unlinked (not git-clean -fd). Blocked names, .git/,
    and Nightshift meta are never touched.
    """
    done: list[str] = []
    repo_resolved = repo.resolve()
    for rel in paths:
        if Path(rel).is_absolute():
            done.append(f"SKIP (outside repo): {rel}")
            continue
        rel_n = normalize_rel(rel)
        if not rel_n or is_meta_path(rel_n):
            continue
        if is_blocked_rel(rel_n) or is_junk(rel_n):
            done.append(f"SKIP (blocked): {rel_n}")
            continue
        candidate = (repo / rel_n).resolve()
        try:
            candidate.relative_to(repo_resolved)
        except ValueError:
            done.append(f"SKIP (outside repo): {rel_n}")
            continue
        if any(part.lower() == ".git" for part in candidate.relative_to(repo_resolved).parts):
            done.append(f"SKIP (blocked): {rel_n}")
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


def diff_stat_against(repo: Path, base: str, extra: list[str] | None = None) -> str:
    args = ["diff", "--stat", f"{base}...HEAD"]
    if extra:
        args.extend(extra)
    proc = git(repo, *args, check=False)
    return proc.stdout.strip()


def commits_since(repo: Path, base: str) -> str:
    proc = git(repo, "log", "--oneline", f"{base}..HEAD", check=False)
    return proc.stdout.strip()


def commits_touching(repo: Path, base: str, paths: list[str]) -> list[str]:
    if not paths:
        return []
    proc = git(
        repo,
        "log",
        "--format=%h",
        f"{base}..HEAD",
        "--",
        *paths,
        check=False,
    )
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


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
