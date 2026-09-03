"""Hard limits: this is personal-capacity OSS, never employer-shaped."""

from __future__ import annotations

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
    if is_blocked_name(candidate.name):
        raise SafetyError(f"refusing to write {candidate.name}")
    return candidate


def is_blocked_name(name: str) -> bool:
    if name in SNAPSHOT_OK_ENV_NAMES:
        return False
    if name in BLOCKED_WRITE_NAMES:
        return True
    if name.startswith(".env"):
        return True
    return Path(name).suffix.lower() in BLOCKED_WRITE_SUFFIXES


def is_blocked_rel(rel: str) -> bool:
    return is_blocked_name(Path(normalize_rel(rel)).name)


def git_visible_files(repo: Path) -> list[str] | None:
    """Tracked + untracked, minus gitignore. None if git cannot list."""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-co", "--exclude-standard"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return [ln.strip().replace("\\", "/") for ln in proc.stdout.splitlines() if ln.strip()]


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
    return normalize_rel(raw)


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
