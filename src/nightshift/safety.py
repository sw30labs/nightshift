"""Hard limits: this is personal-capacity OSS, never employer-shaped."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .models import SafetyError, normalize_rel

PROTECTED_BRANCHES = frozenset({"main", "master"})
BLOCKED_WRITE_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        "id_rsa",
        "id_ed25519",
        "credentials.json",
        "secrets.json",
    }
)
BLOCKED_WRITE_SUFFIXES = frozenset({".pem", ".p12", ".key"})
SNAPSHOT_OK_ENV_NAMES = frozenset({".env.example", ".env.sample", ".env.template"})

JUNK_PARTS = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "node_modules",
        ".eggs",
        "htmlcov",
        ".DS_Store",
    }
)
JUNK_SUFFIXES = frozenset({".pyc", ".pyo", ".egg-info", ".coverage"})
NEVER_COMMIT_META = frozenset(
    {
        ".nightshift/events.jsonl",
        ".nightshift/status.json",
    }
)
IN_PROGRESS_NAMES = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "rebase-merge",
    "rebase-apply",
    "BISECT_LOG",
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


def _has_git_part(parts: tuple[str, ...]) -> bool:
    return any(part.lower() == ".git" for part in parts)


def assert_inside_repo(repo: Path, rel: str) -> Path:
    repo_r = resolve_repo(repo)
    norm = normalize_job_rel(rel)
    if _has_git_part(Path(norm).parts):
        raise SafetyError("writer may not touch .git/")
    if is_blocked_rel(norm):
        raise SafetyError(f"refusing to write protected path: {rel}")
    candidate = (repo_r / norm).resolve()
    try:
        candidate.relative_to(repo_r)
    except ValueError as exc:
        raise SafetyError(f"path escapes the target repo: {rel}") from exc
    rel_parts = candidate.relative_to(repo_r).parts
    if _has_git_part(rel_parts):
        raise SafetyError("writer may not touch .git/")
    if any(is_blocked_name(part) for part in rel_parts):
        raise SafetyError(f"refusing to write {candidate.name}")
    # The job lock names lexical paths. Following a symlink would let an
    # approved filename silently write some other file (including metadata).
    lexical = repo_r / norm
    if candidate != lexical:
        raise SafetyError(f"writer may not follow symlinks: {rel}")
    return candidate


def is_blocked_name(name: str) -> bool:
    name = name.lower()
    if name in SNAPSHOT_OK_ENV_NAMES:
        return False
    if name in BLOCKED_WRITE_NAMES:
        return True
    if name.startswith(".env"):
        return True
    return Path(name).suffix.lower() in BLOCKED_WRITE_SUFFIXES


def is_blocked_rel(rel: str) -> bool:
    norm = normalize_rel(rel)
    if not norm:
        return False
    if _has_git_part(Path(norm).parts):
        return True
    return any(is_blocked_name(part) for part in Path(norm).parts)


def is_junk(rel: str) -> bool:
    """Build/cache/meta paths that must never be committed or reverted as gold-plating."""
    norm = normalize_rel(rel)
    if not norm:
        return False
    if norm in NEVER_COMMIT_META:
        return True
    if norm.startswith(".nightshift/history/"):
        return True
    parts = set(Path(norm).parts)
    if parts & JUNK_PARTS:
        return True
    suffix = Path(norm).suffix.lower()
    if suffix in JUNK_SUFFIXES:
        return True
    # .egg-info is often a directory name, not a suffix
    if any(part.endswith(".egg-info") for part in Path(norm).parts):
        return True
    return False


def git_visible_files(repo: Path) -> list[str] | None:
    """Tracked + untracked, minus gitignore. None if git cannot list."""
    import subprocess

    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "core.quotepath=false",
                "ls-files",
                "-z",
                "-co",
                "--exclude-standard",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return list(dict.fromkeys(path for path in proc.stdout.split("\0") if path))


def is_meta_path(rel: str) -> bool:
    return normalize_rel(rel).startswith(".nightshift/")


def normalize_job_rel(rel: str) -> str:
    """Posix-relative path for a writer turn. Reject empty, absolute, and .."""
    raw = (rel or "").replace("\\", "/").strip()
    if not raw:
        raise SafetyError("empty path")
    if raw.startswith("/") or raw.startswith("~") or (len(raw) >= 2 and raw[1] == ":"):
        raise SafetyError(f"absolute path refused: {rel}")
    if ".." in Path(raw).parts:
        raise SafetyError(f"path traversal refused: {rel}")
    norm = Path(raw).as_posix()
    if norm == "." or "\0" in norm:
        raise SafetyError(f"invalid file path: {rel}")
    return norm


def _inside_job_paths(norm: str, allowed: list[str]) -> bool:
    if norm in allowed:
        return True
    for item in allowed:
        if item.endswith("/"):
            if norm.startswith(item):
                return True
        elif norm.startswith(item.rstrip("/") + "/"):
            return True
    return False


def assert_job_path(rel: str, paths: list[str]) -> str:
    """Host lock: only the current job's paths[] may be written. Empty paths fail closed."""
    norm = normalize_job_rel(rel)
    allowed: list[str] = []
    for item in paths:
        s = str(item or "").strip()
        if not s:
            continue
        try:
            allowed.append(normalize_job_rel(s))
        except SafetyError:
            continue
    if not allowed:
        raise SafetyError(f"job paths[] is empty; refusing write {norm}")
    if not _inside_job_paths(norm, allowed):
        raise SafetyError(f"write outside job paths[]: {norm}")
    return norm


@dataclass
class TreeState:
    dirty: list[str] = field(default_factory=list)
    in_progress: str | None = None
    detached: bool = False


def tree_state(repo: Path) -> TreeState:
    """Dirty / in-progress / detached HEAD, ignoring Nightshift meta and junk."""
    from .gitops import changed_paths, git

    abs_git = git(repo, "rev-parse", "--absolute-git-dir", check=False)
    git_dir = Path(abs_git.stdout.strip()) if abs_git.returncode == 0 and abs_git.stdout.strip() else repo / ".git"
    in_progress: str | None = None
    for name in IN_PROGRESS_NAMES:
        if (git_dir / name).exists():
            in_progress = name
            break
    detached = git(repo, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0
    dirty: list[str] = []
    for rel in changed_paths(repo):
        if is_meta_path(rel) or is_junk(rel):
            continue
        dirty.append(rel)
    return TreeState(dirty=dirty, in_progress=in_progress, detached=detached)


def assert_clean_tree(repo: Path, *, allow_dirty: bool = False) -> TreeState:
    ts = tree_state(repo)
    if ts.in_progress:
        raise SafetyError(
            f"merge/rebase in progress ({ts.in_progress}); finish or abort it first"
        )
    if ts.detached:
        from .gitops import rev_parse

        sha = rev_parse(repo, "HEAD")[:7]
        raise SafetyError(f"detached HEAD at {sha}; checkout a branch first")
    if ts.dirty and not allow_dirty:
        shown = ", ".join(ts.dirty[:10])
        extra = "" if len(ts.dirty) <= 10 else ", …"
        raise SafetyError(
            f"working tree has {len(ts.dirty)} uncommitted changes "
            f"({shown}{extra}); commit or stash first, or pass --allow-dirty"
        )
    return ts
