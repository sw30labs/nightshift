"""LangGraph cycle: critic_job → writer → host_check → critic_score.

Minute 0 (critic_brief) lives in the runner so the writer never touches
the target before the brief is frozen.
"""

from __future__ import annotations

import json
import py_compile
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence, TypedDict

from .config import Settings
from .gitops import (
    changed_paths,
    commit_paths,
    git,
    log_oneline,
    revert_paths,
    rev_parse,
    working_tree_diff,
)
from .host import check_command_file_tokens, count_failed, parse_pytest, run_check
from .llm import Critic, Writer, persist_meta
from .models import Brief, CheckResult, FrozenBriefError, SafetyError, normalize_rel
from .safety import git_visible_files, is_blocked_rel, is_junk, is_meta_path
from .status import StatusBoard

SKIP_SNAPSHOT_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    ".nightshift",
    ".tox",
}
SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".pyc",
    ".so",
    ".dylib",
    ".whl",
}
SKIP_BODY_NAMES = {
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
    "COPYING",
    "NOTICE",
}
SKIP_BODY_SUFFIXES = {
    ".html",
    ".svg",
    ".mmd",
    ".css",
    ".lock",
    ".min.js",
    ".map",
    ".csv",
    ".ipynb",
} | SKIP_SUFFIXES
CODE_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".rb",
    ".sh",
    ".c",
    ".h",
    ".java",
    ".kt",
    ".swift",
}
TEST_CONF = (
    "tests/conftest.py",
    "conftest.py",
    "pyproject.toml",
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
)

TURNS_REL = ".nightshift/turns.jsonl"


class NightState(TypedDict, total=False):
    repo: str
    branch: str
    brief: dict[str, Any]
    remaining_count: int
    job: str
    job_upgrade_id: int
    turn: int
    last_check: dict[str, Any]
    last_diff: str
    refused: list[str]
    written: list[str]
    halt_reason: str
    brain: str
    check_logs: str
    check_results: list[dict[str, Any]]
    main_ref: str
    main_sha: str
    fail_streak: dict[str, Any]
    job_feedback: dict[str, Any]
    turn_refused: list[str]
    turns_on_job: dict[str, int]
    compile_errors: list[str]
    job_red_ids: dict[str, list[str]]
    job_base: dict[str, str]
    base_ref: str
    base_sha: str
    checks: dict[str, Any]
    turn_started_at: str
    turn_t0: float
    node_secs: dict[str, float]


@dataclass
class NightContext:
    repo: Path
    settings: Settings
    writer: Writer
    critic: Critic
    status: StatusBoard
    clock: Callable[[], datetime]
    deadline: datetime
    explicit: bool = True
    main_ref: str = "main"
    main_sha: str = ""
    base_ref: str = ""
    base_sha: str = ""
    refused: list[str] = field(default_factory=list)
    preexisting: set[str] = field(default_factory=set)
    interpreter: str = ""
    interpreter_source: str = ""
    turn_scratch: dict[str, Any] = field(default_factory=dict)
    lens_hint: str = ""  # "oe" | "de" | ""; set once in freeze_brief, never on turn_scratch


def parse_halt_at(halt_at: str) -> tuple[int, int]:
    try:
        hh_s, mm_s = str(halt_at).split(":", 1)
        if ":" in mm_s:
            raise ValueError
        hh, mm = int(hh_s), int(mm_s)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
        return hh, mm
    except (ValueError, AttributeError, TypeError) as exc:
        raise SafetyError(f"halt_at {halt_at!r} is not HH:MM") from exc


def next_halt(halt_at: str, now: datetime) -> datetime:
    try:
        hh, mm = parse_halt_at(halt_at)
    except SafetyError:
        hh, mm = 6, 0
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < candidate:
        return candidate
    return candidate + timedelta(days=1)


FAIL_STREAK_LIMIT = 3
_EXC_TYPE = re.compile(r"- (\w+(?:Error|Exception|Failed))\b")


def fail_fingerprint(exit_code: int, output: str) -> str:
    """Stable-enough signature of a host failure so identical retries tally."""
    parsed = parse_pytest(output or "")
    ids = sorted(parsed["failed"] | parsed["errors"])
    if ids:
        types: list[str] = []
        for ln in (output or "").splitlines():
            m = _EXC_TYPE.search(ln)
            if m:
                types.append(m.group(1))
        return f"{int(exit_code)}:{','.join(ids)}|{','.join(sorted(set(types)))}"
    needles = (
        "ModuleNotFoundError",
        "ImportError",
        "SyntaxError",
        "file or directory not found",
        "ERROR:",
    )
    picked = ""
    for ln in (output or "").splitlines():
        line = ln.strip()
        if any(n in line for n in needles):
            picked = line
            break
    if not picked:
        picked = " ".join((output or "").split())[:180]
    return f"{int(exit_code)}:{picked}"


def bump_fail_streak(streak: dict[str, Any], upgrade_id: int, fingerprint: str) -> dict[str, Any]:
    same = (
        int(streak.get("upgrade_id") or 0) == int(upgrade_id)
        and str(streak.get("fp") or "") == fingerprint
    )
    count = int(streak.get("count") or 0) + 1 if same else 1
    return {"upgrade_id": int(upgrade_id), "fp": fingerprint, "count": count}


def night_changed_rels(repo: Path, base_sha: str) -> set[str]:
    """Repo-relative paths that differ from the night's parent (base_sha)."""
    names: set[str] = set()
    if base_sha:
        proc = git(repo, "diff", "--name-only", base_sha, check=False)
        for line in proc.stdout.splitlines():
            rel = normalize_rel(line.strip().strip('"'))
            if rel:
                names.add(rel)
    for rel in changed_paths(repo):
        names.add(normalize_rel(rel))
    return names


def job_paths_changed(night_changed: set[str], paths: list[str]) -> bool:
    want = {normalize_rel(p) for p in paths if str(p).strip()}
    if not want:
        return False
    for rel in night_changed:
        norm = normalize_rel(rel)
        if norm in want:
            return True
        for item in want:
            if item.endswith("/"):
                if norm.startswith(item):
                    return True
            elif norm.startswith(item.rstrip("/") + "/"):
                return True
            elif item.startswith(norm.rstrip("/") + "/") or (
                item.startswith(norm + "/") if norm else False
            ):
                # a job path inside a newly added directory still counts
                return True
    return False


def pick_job(
    brief: Brief, turns_on_job: dict[str, int] | None, budget: int
) -> Any:
    remaining = brief.remaining()
    if not remaining:
        return None
    budget = max(1, int(budget or 4))
    turns = turns_on_job or {}

    def _key(u: Any) -> tuple[int, int]:
        n = int(turns.get(str(u.id), 0) or 0)
        return (n // budget, int(u.id))

    return min(remaining, key=_key)


def _skip_body(rel: str, *, focus: set[str], oversized: bool) -> bool:
    name = Path(rel).name
    if name in SKIP_BODY_NAMES:
        return True
    suffix = Path(rel).suffix.lower()
    if suffix in SKIP_BODY_SUFFIXES:
        return True
    if Path(rel).name.endswith(".min.js"):
        return True
    if oversized and rel not in focus:
        return True
    return False


def _list_tree(repo: Path) -> list[str]:
    visible = git_visible_files(repo)
    tree: list[str] = []
    if visible is not None:
        for rel in visible:
            if is_blocked_rel(rel):
                continue
            parts = Path(rel).parts
            if any(part in SKIP_SNAPSHOT_DIRS for part in parts):
                continue
            tree.append(rel)
        return tree
    for path in sorted(repo.rglob("*")):
        rel_parts = path.relative_to(repo).parts
        if any(part in SKIP_SNAPSHOT_DIRS for part in rel_parts):
            continue
        if path.is_file() and not is_blocked_rel(path.relative_to(repo).as_posix()):
            tree.append(path.relative_to(repo).as_posix())
    return tree


def _forum_snapshot_block(forum: dict[str, Any], repo: Path) -> str:
    """Other-repo forum excerpt for the freeze snapshot. PR3 ships the block; until then ''."""
    try:
        from .forum import forum_snapshot_block  # type: ignore[attr-defined]
    except ImportError:
        return ""
    if not callable(forum_snapshot_block):
        return ""
    from .ledger import repo_id

    return str(forum_snapshot_block(forum, exclude_repo_id=repo_id(repo)) or "")


def read_snapshot(
    repo: Path,
    *,
    focus: Sequence[str] = (),
    check_command: str = "",
    max_bytes: int = 350_000,
    home: Path | None = None,
    forum: dict[str, Any] | None = None,
) -> str:
    """Critic/writer snapshot.

    `home=` merges the home ledger shard into the prior-night block on every
    snapshot, so OE from a deleted night branch matches what freeze voids on.
    `forum=` (freeze only; the writer never passes it) appends the ranked
    other-repo excerpt after the ledger block.
    """
    focus_rels = [normalize_rel(p) for p in focus if str(p).strip()]
    focus_set = set(focus_rels)
    chunks: list[str] = [f"# repo {repo}", "## git log", log_oneline(repo, 12)]
    readme = repo / "README.md"
    if readme.is_file():
        chunks.append(
            "## README.md\n"
            + readme.read_text(encoding="utf-8", errors="replace")[:4000]
        )
    tree = _list_tree(repo)
    tree_set = set(tree)

    def _exists(rel: str) -> bool:
        return rel in tree_set and (repo / rel).is_file()

    body_skip: set[str] = set()
    for rel in tree:
        path = repo / rel
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        if _skip_body(rel, focus=focus_set, oversized=size > 40_000):
            body_skip.add(rel)

    tree_lines = [f"{rel} (not shown)" if rel in body_skip else rel for rel in tree[:2000]]
    chunks.append("## tree\n" + "\n".join(tree_lines[:500]))

    ordered: list[str] = []
    seen: set[str] = set()

    def _add(rel: str) -> None:
        rel = normalize_rel(rel)
        if not rel or rel in seen or is_blocked_rel(rel):
            return
        if not _exists(rel) and rel not in focus_set:
            return
        seen.add(rel)
        ordered.append(rel)

    job_files: list[str] = []
    for rel in focus_rels:
        if is_blocked_rel(rel):
            continue
        if _exists(rel):
            job_files.append(rel)
            _add(rel)

    for tok in check_command_file_tokens(check_command):
        if _exists(tok):
            _add(tok)

    if any(rel.startswith("tests/") or Path(rel).name.startswith("test_") for rel in focus_rels):
        for rel in TEST_CONF:
            if _exists(rel):
                _add(rel)

    for rel in focus_rels:
        parent = str(Path(rel).parent).replace("\\", "/")
        if parent in {".", ""}:
            siblings = [t for t in tree if "/" not in t]
        else:
            prefix = parent.rstrip("/") + "/"
            siblings = [
                t
                for t in tree
                if t.startswith(prefix) and t.count("/") == prefix.count("/")
            ]
        for sib in siblings:
            _add(sib)

    rest = [t for t in tree if t not in seen]
    code_out = [
        t
        for t in rest
        if Path(t).suffix.lower() in CODE_SUFFIXES and not t.startswith("tests/")
    ]
    tests = [t for t in rest if t.startswith("tests/")]
    other = [t for t in rest if t not in code_out and t not in tests]
    for group in (code_out, tests, other):
        for rel in group:
            _add(rel)

    shown = [
        rel
        for rel in ordered
        if rel not in body_skip or rel in focus_set
    ]
    chunks.append("## shown in full\n" + "\n".join(shown[:400]))

    from .ledger import ledger_snapshot_block, load_ledger

    block = ledger_snapshot_block(load_ledger(repo, home=home))
    if block.strip():
        chunks.append(block.rstrip())
    if forum is not None:
        forum_block = _forum_snapshot_block(forum, repo)
        if forum_block.strip():
            chunks.append(forum_block.rstrip())

    used = sum(len(c) for c in chunks)
    for rel in ordered:
        path = repo / rel
        if not path.is_file():
            continue
        if is_blocked_rel(rel):
            continue
        is_job = rel in focus_set
        if rel in body_skip and not is_job:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        cap = 80_000 if is_job else 80_000
        if len(text) > cap:
            text = text[:cap] + "\n…truncated"
        heading = f"## job file {rel}" if is_job else f"## file {rel}"
        piece = f"{heading}\n{text}"
        if not is_job and used + len(piece) > max_bytes:
            continue
        chunks.append(piece)
        used += len(piece)
        if used >= max_bytes and not is_job:
            # still emit remaining job files
            continue
    return "\n\n".join(chunks)


def path_allowed(rel: str, allowed: set[str]) -> bool:
    norm = normalize_rel(rel)
    if is_meta_path(norm):
        return True
    if norm in allowed:
        return True
    for item in allowed:
        item_n = normalize_rel(item)
        if not item_n:
            continue
        if item_n.endswith("/"):
            if norm.startswith(item_n):
                return True
        elif norm.startswith(item_n.rstrip("/") + "/"):
            return True
    return False


def path_too_broad(rel: str, allowed: set[str]) -> bool:
    """True if reverting rel would wipe an allowed job path (ancestor / '.')."""
    norm = normalize_rel(rel)
    if norm in {"", ".", "/"}:
        return True
    for item in allowed:
        item_n = normalize_rel(item)
        if not item_n:
            continue
        if item_n == norm:
            continue
        if item_n.startswith(norm.rstrip("/") + "/"):
            return True
    return False


def unapproved_paths(changed: list[str], allowed: set[str]) -> list[str]:
    out = []
    for rel in changed:
        norm = normalize_rel(rel)
        if is_junk(norm) or is_meta_path(norm):
            continue
        if not path_allowed(rel, allowed):
            out.append(rel)
    return out


def append_turn_row(repo: Path, row: dict[str, Any]) -> None:
    path = repo / TURNS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def load_turns(repo: Path) -> list[dict[str, Any]]:
    path = repo / TURNS_REL
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    except OSError:
        return []
    return rows


class LoopNodes:
    def __init__(self, ctx: NightContext) -> None:
        self.ctx = ctx

    def _brief(self, state: NightState) -> Brief:
        return Brief.from_dict(state["brief"])

    def _store_brief(self, brief: Brief) -> None:
        persist_meta(
            self.ctx.repo,
            ".nightshift/brief.json",
            json.dumps(brief.to_dict(), indent=2) + "\n",
        )

    def _mark(self, node: str, state: NightState) -> float:
        t0 = time.monotonic()
        secs = dict(state.get("node_secs") or {})
        secs[f"_{node}_t0"] = t0
        self.ctx.turn_scratch[f"{node}_t0"] = t0
        return t0

    def _elapsed(self, node: str) -> float:
        t0 = float(self.ctx.turn_scratch.get(f"{node}_t0") or time.monotonic())
        return round(time.monotonic() - t0, 3)

    def critic_job(self, state: NightState) -> dict[str, Any]:
        """one-line job from the frozen brief"""
        self.ctx.status.update(brain="critic", state="running")
        self._mark("critic_job", state)
        brief = self._brief(state)
        turns_on_job = dict(state.get("turns_on_job") or {})
        feedback = dict(state.get("job_feedback") or {})
        picked = pick_job(brief, turns_on_job, self.ctx.settings.job_turns)
        uid = int(picked.id) if picked is not None else 0
        prev_uid = int(feedback.get("upgrade_id") or 0)
        if uid and uid != prev_uid:
            feedback = {}
        job_base = dict(state.get("job_base") or {})
        if uid and str(uid) not in job_base:
            job_base[str(uid)] = rev_parse(self.ctx.repo, "HEAD")
        uid, job = self.ctx.critic.job_line(
            brief, upgrade_id=uid or None, feedback=feedback or None
        )
        if uid:
            turns_on_job[str(uid)] = int(turns_on_job.get(str(uid), 0) or 0) + 1
        turn = int(state.get("turn") or 0) + 1
        started = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        self.ctx.turn_scratch = {
            "started_at": started,
            "critic_job": self._elapsed("critic_job"),
            "upgrade_id": uid,
            "job": job,
        }
        self.ctx.status.update(job=job, job_upgrade_id=uid, turn=turn, brain="critic")
        return {
            "brain": "critic",
            "job": job,
            "job_upgrade_id": uid,
            "turn": turn,
            "remaining_count": brief.remaining_count,
            "turns_on_job": turns_on_job,
            "job_feedback": feedback,
            "job_base": job_base,
            "turn_started_at": started,
            "compile_errors": [],
            "turn_refused": [],
        }

    def writer(self, state: NightState) -> dict[str, Any]:
        """edit files on the night branch"""
        self.ctx.status.update(brain="writer", state="running")
        self._mark("writer", state)
        brief = self._brief(state)
        job = str(state.get("job") or "")
        uid = int(state.get("job_upgrade_id") or 0)
        locked = next((u for u in brief.upgrades if u.id == uid), None)
        if locked is None:
            locked = brief.remaining()[0] if brief.remaining() else None
        focus = list(locked.paths) if locked is not None else []
        check_command = locked.check_command if locked is not None else ""
        snapshot = read_snapshot(
            self.ctx.repo,
            focus=focus,
            check_command=check_command,
            home=self.ctx.settings.home,  # never forum=: the writer sees no other repo
        )
        feedback = dict(state.get("job_feedback") or {})
        try:
            result = self.ctx.writer.apply_job(
                job,
                brief,
                snapshot,
                job_upgrade_id=uid,
                feedback=feedback or None,
            )
        except TimeoutError as exc:
            note = f"writer timed out ({exc}); will retry"
            if note not in self.ctx.refused:
                self.ctx.refused.append(note)
            self.ctx.turn_scratch["writer"] = self._elapsed("writer")
            self.ctx.turn_scratch["written"] = []
            self.ctx.turn_scratch["writer_refused"] = [note]
            return {
                "brain": "writer",
                "written": [],
                "refused": list(self.ctx.refused),
                "turn_refused": [note],
                "compile_errors": [],
                "remaining_count": brief.remaining_count,
            }
        compile_errors: list[str] = []
        written = list(result.written)
        turn_refused = list(result.refused)
        surviving: list[str] = []
        for rel in written:
            if not rel.endswith(".py"):
                surviving.append(rel)
                continue
            path = self.ctx.repo / rel
            try:
                py_compile.compile(str(path), doraise=True)
                surviving.append(rel)
            except py_compile.PyCompileError as exc:
                msg = str(exc.msg if hasattr(exc, "msg") else exc)
                line = ""
                m = re.search(r"line (\d+)", msg)
                if m:
                    line = m.group(1)
                note = f"{rel}: SyntaxError line {line or '?'}: {msg} -- write reverted"
                compile_errors.append(note)
                turn_refused.append(note)
                tracked = git(self.ctx.repo, "ls-files", "--", rel, check=False)
                if tracked.stdout.strip():
                    git(self.ctx.repo, "checkout", "--", rel)
                elif path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        pass
        for note in turn_refused:
            if note not in self.ctx.refused:
                self.ctx.refused.append(note)
        self.ctx.turn_scratch["writer"] = self._elapsed("writer")
        self.ctx.turn_scratch["written"] = surviving
        self.ctx.turn_scratch["writer_refused"] = turn_refused
        self.ctx.turn_scratch["compile_errors"] = compile_errors
        return {
            "brain": "writer",
            "written": surviving,
            "refused": list(self.ctx.refused),
            "turn_refused": turn_refused,
            "compile_errors": compile_errors,
            "remaining_count": brief.remaining_count,
        }

    def host_check(self, state: NightState) -> dict[str, Any]:
        """run the real check commands"""
        self.ctx.status.update(brain="host", state="running")
        self._mark("host_check", state)
        brief = self._brief(state)
        job_uid = int(state.get("job_upgrade_id") or 0)
        results: list[CheckResult] = []
        for upgrade in brief.upgrades:
            if upgrade.void:
                continue
            results.append(
                run_check(self.ctx.repo, upgrade, self.ctx.settings.check_timeout)
            )
        logs = []
        last: dict[str, Any] = {}
        checks: dict[str, Any] = {}
        job_last: dict[str, Any] = {}
        first_fail: dict[str, Any] = {}
        for row in results:
            logs.append(
                f"upgrade {row.upgrade_id} exit={row.exit_code} ok={row.ok}\n"
                f"$ {row.command}\n{row.output}"
            )
            payload = {
                "upgrade_id": row.upgrade_id,
                "command": row.command,
                "exit_code": row.exit_code,
                "ok": row.ok,
                "output": row.output[-2000:],
                "tail": row.output[-600:],
            }
            checks[str(row.upgrade_id)] = {
                "ok": row.ok,
                "exit_code": row.exit_code,
                "tail": row.output[-600:],
            }
            last = payload
            if row.upgrade_id == job_uid:
                job_last = payload
            if not row.ok and not first_fail:
                first_fail = payload
        if job_last:
            last = job_last
        elif first_fail:
            last = first_fail
        joined = "\n\n---\n\n".join(logs)
        self.ctx.status.update(last_check=last, checks=checks, brain="host")
        self.ctx.turn_scratch["host_check"] = self._elapsed("host_check")
        return {
            "brain": "host",
            "check_logs": joined,
            "last_check": last,
            "check_results": [row.__dict__ for row in results],
            "checks": checks,
            "last_diff": working_tree_diff(self.ctx.repo),
        }

    def critic_score(self, state: NightState) -> dict[str, Any]:
        """score, slash, revert gold-plating"""
        self.ctx.status.update(brain="critic", state="running")
        self._mark("critic_score", state)
        brief = self._brief(state)
        results_raw = state.get("check_results") or []
        by_id: dict[int, dict[str, Any]] = {}
        for row in results_raw:
            by_id[int(row["upgrade_id"])] = row
        job_uid = int(state.get("job_upgrade_id") or 0)
        base_sha = str(
            (state.get("job_base") or {}).get(str(job_uid))
            or state.get("base_sha")
            or self.ctx.base_sha
            or state.get("main_sha")
            or self.ctx.main_sha
            or ""
        )
        opinion = self.ctx.critic.opinion(
            brief,
            str(state.get("last_diff") or ""),
            str(state.get("check_logs") or ""),
            job_upgrade_id=job_uid,
        )
        for uid in opinion.get("passed_ids") or []:
            row = by_id.get(int(uid))
            if row and not row.get("ok"):
                self.ctx.refused.append(
                    f"critic claimed upgrade {uid} passed; host check failed"
                )
        current = next((u for u in brief.upgrades if u.id == job_uid), None)
        allowed = {
            normalize_rel(p)
            for p in (current.paths if current is not None else [])
            if str(p).strip()
        }
        changed = [
            p
            for p in changed_paths(self.ctx.repo)
            if normalize_rel(p) not in self.ctx.preexisting
        ]
        revert = unapproved_paths(changed, allowed)
        for extra in opinion.get("revert_paths") or []:
            extra_n = normalize_rel(str(extra))
            if not extra_n or extra_n in {normalize_rel(r) for r in revert}:
                continue
            if path_allowed(extra_n, allowed) or path_too_broad(extra_n, allowed):
                continue
            if extra_n not in {normalize_rel(c) for c in changed}:
                continue
            revert.append(extra)
        reverted = revert_paths(self.ctx.repo, revert)
        for path in reverted:
            note = f"reverted unapproved path {path}"
            if note not in self.ctx.refused:
                self.ctx.refused.append(note)
        night_changed = night_changed_rels(self.ctx.repo, base_sha) - self.ctx.preexisting
        job_red_ids = dict(state.get("job_red_ids") or {})
        row = by_id.get(job_uid)
        required: set[str] | None = None
        if row and not row.get("ok"):
            parsed = parse_pytest(str(row.get("output") or ""))
            ids = [i for i in sorted(parsed["failed"] | parsed["errors"]) if "::" in i]
            if ids and str(job_uid) not in job_red_ids:
                job_red_ids[str(job_uid)] = ids
        if str(job_uid) in job_red_ids:
            required = set(job_red_ids[str(job_uid)])
        apply_host_truth(
            brief,
            [
                CheckResult(
                    upgrade_id=int(r["upgrade_id"]),
                    command=str(r.get("command") or ""),
                    ok=bool(r.get("ok")),
                    exit_code=int(r.get("exit_code") or 0),
                    output=str(r.get("output") or ""),
                )
                for r in results_raw
            ],
            job_id=job_uid,
            night_changed=night_changed,
            required_ids=required,
        )
        current = next((u for u in brief.upgrades if u.id == job_uid), None)
        streak = dict(state.get("fail_streak") or {})
        written = list(state.get("written") or [])
        if (
            current is not None
            and not current.void
            and row
            and not row.get("ok")
            and written
        ):
            fp = fail_fingerprint(int(row.get("exit_code") or 0), str(row.get("output") or ""))
            streak = bump_fail_streak(streak, job_uid, fp)
            if int(streak.get("count") or 0) >= FAIL_STREAK_LIMIT:
                try:
                    brief.void_upgrade(
                        job_uid,
                        "same_host_failure",
                    )
                    note = (
                        f"upgrade {job_uid} voided: same host failure "
                        f"{FAIL_STREAK_LIMIT} times; unlocking next job"
                    )
                    if note not in self.ctx.refused:
                        self.ctx.refused.append(note)
                    streak = {}
                except FrozenBriefError as exc:
                    note = f"could not void upgrade {job_uid}: {exc}"
                    if note not in self.ctx.refused:
                        self.ctx.refused.append(note)
        elif current is not None and row and row.get("ok"):
            streak = {}
        turn = int(state.get("turn") or 0)
        if current is not None and not current.void and row and not row.get("ok"):
            first = fail_fingerprint(
                int(row.get("exit_code") or 0), str(row.get("output") or "")
            )
            needle = first.split(":", 1)[-1][:120]
            current.note = f"turn {turn}: exit {row.get('exit_code')}; {needle}"
        elif current is not None and current.done:
            current.note = f"landed turn {turn}"
        if (
            current is not None
            and not current.void
            and not current.done
            and row
            and row.get("ok")
            and required
        ):
            parsed = parse_pytest(str(row.get("output") or ""))
            missing = sorted(required - parsed["passed"])
            if missing:
                note = (
                    f"upgrade {job_uid} green but tests vanished: "
                    f"{', '.join(missing)}; not marking done"
                )
                if note not in self.ctx.refused:
                    self.ctx.refused.append(note)
        for upgrade in brief.upgrades:
            if upgrade.done:
                row_u = by_id.get(upgrade.id)
                if row_u and not row_u.get("ok"):
                    note = f"upgrade {upgrade.id} check failed after done; leaving done"
                    if note not in self.ctx.refused:
                        self.ctx.refused.append(note)
            elif (
                upgrade.id == job_uid
                and not upgrade.void
                and row
                and row.get("ok")
                and not job_paths_changed(night_changed, upgrade.paths)
            ):
                note = (
                    f"upgrade {upgrade.id} check passed but paths[] unchanged "
                    "this night; not marking done"
                )
                if note not in self.ctx.refused:
                    self.ctx.refused.append(note)
        for note in opinion.get("notes") or []:
            if note not in self.ctx.refused:
                self.ctx.refused.append(str(note))
        self._store_brief(brief)
        job = str(state.get("job") or "pass")[:72]
        sha = commit_paths(
            self.ctx.repo,
            f"nightshift: turn {turn} — {job}",
            None,
            exclude=self.ctx.preexisting,
        )
        halt_reason = str(state.get("halt_reason") or "")
        if opinion.get("halt"):
            halt_reason = halt_reason or "critic"
        remaining = brief.remaining_count
        still_open = current is not None and not current.done and not current.void
        job_feedback: dict[str, Any] = {}
        if still_open and row:
            output = str(row.get("output") or "")
            if len(output) > 6000:
                clipped = output[:2500] + "\n…\n" + output[-3500:]
            else:
                clipped = output
            job_feedback = {
                "upgrade_id": job_uid,
                "turn": turn,
                "command": row.get("command"),
                "exit_code": row.get("exit_code"),
                "output": clipped,
                "writer_refused": list(state.get("turn_refused") or []),
                "critic_notes": list(opinion.get("notes") or []),
                "compile_errors": list(state.get("compile_errors") or []),
            }
        void_info = None
        if current is not None and current.void:
            void_info = {"id": current.id, "reason": current.void_reason}
        parsed_pass: list[str] = []
        if row:
            parsed_pass = sorted(parse_pytest(str(row.get("output") or ""))["passed"])
        tape = {
            "turn": turn,
            "started_at": state.get("turn_started_at") or "",
            "ended_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "upgrade_id": job_uid,
            "job": job[:200],
            "written": list(state.get("written") or []),
            "writer_refused": list(state.get("turn_refused") or []),
            "compile_errors": list(state.get("compile_errors") or []),
            "check": {
                "exit_code": (row or {}).get("exit_code"),
                "ok": (row or {}).get("ok"),
                "fingerprint": (
                    fail_fingerprint(
                        int((row or {}).get("exit_code") or 0),
                        str((row or {}).get("output") or ""),
                    )
                    if row
                    else ""
                ),
                "tail": str((row or {}).get("output") or "")[-400:],
            },
            "reverted": list(reverted),
            "critic_notes": list(opinion.get("notes") or []),
            "passed_ids_claimed": list(opinion.get("passed_ids") or []),
            "done_ids_after": [u.id for u in brief.upgrades if u.done],
            "void": void_info,
            "commit": (sha or "")[:7],
            "secs": {
                "critic_job": float(self.ctx.turn_scratch.get("critic_job") or 0),
                "writer": float(self.ctx.turn_scratch.get("writer") or 0),
                "host_check": float(self.ctx.turn_scratch.get("host_check") or 0),
                "critic_score": self._elapsed("critic_score"),
            },
        }
        append_turn_row(self.ctx.repo, tape)
        turns_mirror = (list(self.ctx.status.current.turns) + [tape])[-60:]
        self.ctx.status.update(
            remaining_count=remaining,
            refused=list(self.ctx.refused),
            brain="critic",
            brief=brief.to_dict(),
            fail_streak=streak,
            turns=turns_mirror,
        )
        return {
            "brain": "critic",
            "brief": brief.to_dict(),
            "remaining_count": remaining,
            "refused": list(self.ctx.refused),
            "halt_reason": halt_reason,
            "written": [],
            "fail_streak": streak,
            "job_feedback": job_feedback,
            "job_red_ids": job_red_ids,
            "passed_ids": parsed_pass,
        }


def build_cycle_app(nodes: LoopNodes):
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return None
    graph = StateGraph(NightState)
    graph.add_node("critic_job", nodes.critic_job)
    graph.add_node("writer", nodes.writer)
    graph.add_node("host_check", nodes.host_check)
    graph.add_node("critic_score", nodes.critic_score)
    graph.add_edge(START, "critic_job")
    graph.add_edge("critic_job", "writer")
    graph.add_edge("writer", "host_check")
    graph.add_edge("host_check", "critic_score")
    graph.add_edge("critic_score", END)
    return graph.compile()


def run_cycle(nodes: LoopNodes, state: NightState) -> NightState:
    merged: dict[str, Any] = dict(state)
    for fn in (nodes.critic_job, nodes.writer, nodes.host_check, nodes.critic_score):
        merged.update(fn(merged))  # type: ignore[arg-type]
    return merged  # type: ignore[return-value]


def apply_host_truth(
    brief: Brief,
    results: list[CheckResult],
    *,
    job_id: int = 0,
    night_changed: set[str] | None = None,
    required_ids: set[str] | None = None,
) -> None:
    """Mark done only for the current job, and only if its paths changed tonight.

    Pre-existing green checks on other jobs are not evidence the writer landed them.
    """
    by_id = {r.upgrade_id: r for r in results}
    changed = night_changed if night_changed is not None else set()
    for upgrade in brief.upgrades:
        if upgrade.void:
            continue
        row = by_id.get(upgrade.id)
        if upgrade.done:
            continue
        if upgrade.id != int(job_id or 0):
            continue
        if not (row and row.ok):
            continue
        if not job_paths_changed(changed, upgrade.paths):
            continue
        if required_ids:
            parsed = parse_pytest(row.output or "")
            if not required_ids.issubset(parsed["passed"]):
                continue
        upgrade.done = True
