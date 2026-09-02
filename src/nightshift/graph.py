"""LangGraph cycle: critic_job → writer → host_check → critic_score.

Minute 0 (critic_brief) lives in the runner so the writer never touches
the target before the brief is frozen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TypedDict

from .config import Settings
from .gitops import changed_paths, commit_paths, log_oneline, revert_paths, working_tree_diff
from .host import run_check
from .llm import Critic, Writer, persist_meta
from .models import Brief, CheckResult, normalize_rel
from .safety import git_visible_files, is_blocked_rel, is_meta_path
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
    refused: list[str] = field(default_factory=list)


def next_halt(halt_at: str, now: datetime) -> datetime:
    try:
        hh_s, mm_s = halt_at.split(":", 1)
        hh, mm = int(hh_s), int(mm_s)
    except (ValueError, AttributeError):
        hh, mm = 6, 0
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < candidate:
        return candidate
    return candidate + timedelta(days=1)


def read_snapshot(repo: Path, max_bytes: int = 350_000) -> str:
    chunks: list[str] = [f"# repo {repo}", "## git log", log_oneline(repo, 12)]
    readme = repo / "README.md"
    if readme.is_file():
        chunks.append("## README.md\n" + readme.read_text(encoding="utf-8", errors="replace")[:8000])
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
    else:
        for path in sorted(repo.rglob("*")):
            rel_parts = path.relative_to(repo).parts
            if any(part in SKIP_SNAPSHOT_DIRS for part in rel_parts):
                continue
            if path.is_file() and not is_blocked_rel(path.relative_to(repo).as_posix()):
                tree.append(path.relative_to(repo).as_posix())
    chunks.append("## tree\n" + "\n".join(tree[:500]))

    used = sum(len(c) for c in chunks)
    for rel in tree:
        if used >= max_bytes:
            break
        path = repo / rel
        if is_blocked_rel(rel):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) > 80_000:
            text = text[:80_000] + "\n…truncated"
        block = f"## file {rel}\n{text}"
        chunks.append(block)
        used += len(block)
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


SKIP_REVERT_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
SKIP_REVERT_SUFFIXES = {".pyc", ".pyo"}


def unapproved_paths(changed: list[str], allowed: set[str]) -> list[str]:
    out = []
    for rel in changed:
        norm = normalize_rel(rel)
        parts = set(Path(norm).parts)
        if parts & SKIP_REVERT_PARTS:
            continue
        if Path(norm).suffix in SKIP_REVERT_SUFFIXES:
            continue
        if not path_allowed(rel, allowed):
            out.append(rel)
    return out


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

    def critic_job(self, state: NightState) -> dict[str, Any]:
        """one-line job from the frozen brief"""
        self.ctx.status.update(brain="critic", state="running")
        brief = self._brief(state)
        uid, job = self.ctx.critic.job_line(brief)
        turn = int(state.get("turn") or 0) + 1
        return {
            "brain": "critic",
            "job": job,
            "job_upgrade_id": uid,
            "turn": turn,
            "remaining_count": brief.remaining_count,
        }

    def writer(self, state: NightState) -> dict[str, Any]:
        """edit files on the night branch"""
        self.ctx.status.update(brain="writer", state="running")
        brief = self._brief(state)
        job = str(state.get("job") or "")
        snapshot = read_snapshot(self.ctx.repo)
        try:
            result = self.ctx.writer.apply_job(job, brief, snapshot)
        except TimeoutError as exc:
            note = f"writer timed out ({exc}); will retry"
            if note not in self.ctx.refused:
                self.ctx.refused.append(note)
            return {
                "brain": "writer",
                "written": [],
                "refused": list(self.ctx.refused),
                "remaining_count": brief.remaining_count,
            }
        for note in result.refused:
            if note not in self.ctx.refused:
                self.ctx.refused.append(note)
        return {
            "brain": "writer",
            "written": result.written,
            "refused": list(self.ctx.refused),
            "remaining_count": brief.remaining_count,
        }

    def host_check(self, state: NightState) -> dict[str, Any]:
        """run the real check commands"""
        self.ctx.status.update(brain="host", state="running")
        brief = self._brief(state)
        results: list[CheckResult] = []
        for upgrade in brief.upgrades:
            results.append(
                run_check(self.ctx.repo, upgrade, self.ctx.settings.check_timeout)
            )
        logs = []
        last: dict[str, Any] = {}
        for row in results:
            logs.append(
                f"upgrade {row.upgrade_id} exit={row.exit_code} ok={row.ok}\n"
                f"$ {row.command}\n{row.output}"
            )
            last = {
                "upgrade_id": row.upgrade_id,
                "command": row.command,
                "exit_code": row.exit_code,
                "ok": row.ok,
                "output": row.output[-2000:],
            }
        joined = "\n\n---\n\n".join(logs)
        self.ctx.status.update(last_check=last, brain="host")
        return {
            "brain": "host",
            "check_logs": joined,
            "last_check": last,
            "check_results": [row.__dict__ for row in results],
            "last_diff": working_tree_diff(self.ctx.repo),
        }

    def critic_score(self, state: NightState) -> dict[str, Any]:
        """score, slash, revert gold-plating"""
        self.ctx.status.update(brain="critic", state="running")
        brief = self._brief(state)
        results_raw = state.get("check_results") or []
        by_id: dict[int, dict[str, Any]] = {}
        for row in results_raw:
            by_id[int(row["upgrade_id"])] = row
        # Host output is truth. Critic opinion cannot mark a failing check done.
        for upgrade in brief.upgrades:
            row = by_id.get(upgrade.id)
            upgrade.done = bool(row and row.get("ok"))
        opinion = self.ctx.critic.opinion(
            brief,
            str(state.get("last_diff") or ""),
            str(state.get("check_logs") or ""),
        )
        for uid in opinion.get("passed_ids") or []:
            row = by_id.get(int(uid))
            if row and not row.get("ok"):
                self.ctx.refused.append(
                    f"critic claimed upgrade {uid} passed; host check failed"
                )
        allowed = brief.allowed_paths()
        changed = changed_paths(self.ctx.repo)
        revert = unapproved_paths(changed, allowed)
        for extra in opinion.get("revert_paths") or []:
            if extra and extra not in revert and not path_allowed(extra, allowed):
                revert.append(extra)
        reverted = revert_paths(self.ctx.repo, revert)
        for path in reverted:
            note = f"reverted unapproved path {path}"
            if note not in self.ctx.refused:
                self.ctx.refused.append(note)
        for note in opinion.get("notes") or []:
            if note not in self.ctx.refused:
                self.ctx.refused.append(str(note))
        self._store_brief(brief)
        job = str(state.get("job") or "pass")[:72]
        turn = int(state.get("turn") or 0)
        commit_paths(
            self.ctx.repo,
            f"nightshift: turn {turn} — {job}",
            None,
        )
        halt_reason = str(state.get("halt_reason") or "")
        if opinion.get("halt"):
            halt_reason = halt_reason or "critic"
        remaining = brief.remaining_count
        self.ctx.status.update(
            remaining_count=remaining,
            refused=list(self.ctx.refused),
            brain="critic",
        )
        return {
            "brain": "critic",
            "brief": brief.to_dict(),
            "remaining_count": remaining,
            "refused": list(self.ctx.refused),
            "halt_reason": halt_reason,
            "written": [],
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


def apply_host_truth(brief: Brief, results: list[CheckResult]) -> None:
    """Decrement only what the host actually passed. Used by unit tests."""
    by_id = {r.upgrade_id: r for r in results}
    for upgrade in brief.upgrades:
        row = by_id.get(upgrade.id)
        upgrade.done = bool(row and row.ok)
    # A critic saying "passed" cannot override a failing host check.
    for upgrade in brief.upgrades:
        row = by_id.get(upgrade.id)
        if row and not row.ok:
            upgrade.done = False
