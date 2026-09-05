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
    literal: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["git", *(["--literal-pathspecs"] if literal else []), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        errors="surrogateescape",
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
    if branch == "HEAD":
        raise SafetyError("refusing to commit with detached HEAD")
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
        "-z",
        "--untracked-files=all",
    ).stdout.split("\0")
    out: list[str] = []
    seen: set[str] = set()
    records = iter(porcelain)
    for line in records:
        if not line:
            continue
        names = [line[3:]]
        if "R" in line[:2] or "C" in line[:2]:
            source = next(records, "")
            # A rename changes both paths; a copy leaves its source intact.
            if "R" in line[:2]:
                names.append(source)
        for rel in names:
            if rel and rel not in seen:
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
    skip = set(exclude or ())
    repo_root = repo.resolve()

    def safe_path(rel: str) -> bool:
        path = Path(rel)
        if not rel or path.is_absolute() or ".." in path.parts:
            return False
        if is_blocked_rel(rel) or is_junk(rel):
            return False
        if any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in skip):
            return False
        try:
            # Check parents without dereferencing a final symlink: git stages
            # the link itself, not the file it points to.
            parent = (repo_root / path).parent.resolve().relative_to(repo_root)
        except (OSError, ValueError, RuntimeError):
            return False
        return not is_blocked_rel(parent.as_posix())

    if paths is not None:
        requested = [p for p in paths if safe_path(p)]
        if not requested:
            return None
        # Expand directories before filtering; `git add directory` could stage
        # secrets or excluded user work nested beneath an otherwise safe path.
        candidates: list[str] = []
        for args in (
            ("ls-files", "-z", "--cached", "--others", "--exclude-standard"),
            ("ls-files", "-z", "--others", "--ignored", "--exclude-standard"),
            ("diff", "--name-only", "-z", "HEAD"),
        ):
            candidates.extend(git(repo, *args, "--", *requested, literal=True).stdout.split("\0"))
    else:
        candidates = changed_paths(repo)
    to_add = list(dict.fromkeys(p for p in candidates if safe_path(p)))
    if not to_add:
        return None
    indexed = set(git(repo, "ls-files", "-z", "--", *to_add, literal=True).stdout.split("\0"))
    # `git rm` already removed staged deletions from the index. Passing those
    # absent paths to `git add` fails; retain them only in the commit scope.
    stage = [
        p for p in to_add
        if p in indexed or (repo / p).exists() or (repo / p).is_symlink()
    ]
    for i in range(0, len(stage), 500):
        # Explicit metadata may be ignored; automatic commits respect ignores.
        flags = ["-f"] if paths is not None else []
        git(repo, "add", *flags, "--", *stage[i : i + 500], literal=True)
    staged = git(repo, "diff", "--cached", "--name-only", "-z", "--", *to_add, literal=True).stdout
    if not staged:
        return None
    # --only leaves unrelated entries in the user's index staged for later.
    git(repo, "commit", "--only", "-m", message, "--", *to_add, extra_env=NIGHTSHIFT_GIT_ENV, literal=True)
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
        rel_n = rel
        if ".." in Path(rel_n).parts:
            done.append(f"SKIP (outside repo): {rel_n}")
            continue
        if not rel_n or is_meta_path(rel_n):
            continue
        if is_blocked_rel(rel_n) or is_junk(rel_n):
            done.append(f"SKIP (blocked): {rel_n}")
            continue
        candidate = repo_resolved / rel_n
        try:
            parent = candidate.parent.resolve().relative_to(repo_resolved)
        except (OSError, ValueError, RuntimeError):
            done.append(f"SKIP (outside repo): {rel_n}")
            continue
        if is_blocked_rel(parent.as_posix()):
            done.append(f"SKIP (blocked): {rel_n}")
            continue
        if candidate.is_dir() and not candidate.is_symlink():
            continue
        tracked = git(repo, "cat-file", "-t", f"HEAD:{rel_n}", check=False)
        if tracked.returncode == 0 and tracked.stdout.strip() == "blob":
            git(repo, "restore", "--source=HEAD", "--staged", "--worktree", "--", rel_n, literal=True)
            done.append(rel_n)
        else:
            # Unstage newly added files without touching any other index entry.
            indexed = git(repo, "ls-files", "-z", "--", rel_n, literal=True).stdout
            if indexed:
                git(repo, "reset", "-q", "HEAD", "--", rel_n, literal=True)
            if candidate.is_file() or candidate.is_symlink():
                candidate.unlink()
                done.append(rel_n)
            # never rmtree directories blindly
    return done


def last_commit_unix(repo: Path) -> int:
    """`git log -1 --format=%ct`. 0 when the repo has no commits or git fails."""
    proc = git(repo, "log", "-1", "--format=%ct", check=False)
    raw = (proc.stdout or "").strip().splitlines()
    if not raw:
        return 0
    try:
        return int(raw[0].strip())
    except ValueError:
        return 0


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
        literal=True,
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
