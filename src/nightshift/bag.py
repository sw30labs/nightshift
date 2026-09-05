"""Tonight's bag: select a queue, lock it, run nights one after another.

`bag.json` is the durable lock (`state=="running"` + live `runner_pid`).
`status.json` stays the current-night board. Merge them at read time.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .cmm import score_repo
from .config import Settings
from .forum import atomic_write_json, forum_enabled, load_forum, publish_error_stub, with_home_lock
from .gitops import current_branch, last_commit_unix
from .ledger import repo_id
from .models import SafetyError
from .observe import log, ralph_loop, start as observe_start, stop_active
from .repos import find_repos
from .safety import is_nightshift_repo, resolve_repo, tree_state
from .status import StatusBoard

BAG_REL = "bag.json"
BAG_SCHEMA = 1
BAG_SIZE_DEFAULT = 2
BAG_SIZE_MAX = 3
BAG_MIN_MINUTES = 30
PRIOR_REL = "prior.json"
HOLE_SCORE = {0: 4, 1: 3, 2: 1, 3: 0, 4: 0, 5: 0}


@dataclass
class BagTarget:
    path: Path
    name: str
    repo_id: str
    role: str  # "meta" | "portfolio"
    cmm_level: int
    last_commit_unix: int
    skip_reason: str = ""


@dataclass
class BagPlan:
    bag_id: str
    targets: list[BagTarget]
    skipped: list[BagTarget]
    size: int
    skip_meta: bool
    meta_last: bool


def clamp_bag_size(n: int | None, default: int = BAG_SIZE_DEFAULT) -> int:
    if n is None:
        n = default
    try:
        size = int(n)
    except (TypeError, ValueError):
        size = default
    return max(1, min(BAG_SIZE_MAX, size))


def package_checkout() -> Path | None:
    """The checkout this package is imported from, when it is Nightshift itself.

    Tests patch this to None (N7) so a mock bag never branches the operator checkout.
    """
    root = Path(__file__).resolve().parents[2]
    marker = root / ".git"
    if (marker.is_dir() or marker.is_file()) and is_nightshift_repo(root):
        return root
    return None


def pid_alive(pid: int | None, *, self_pid: int | None = None) -> bool:
    """True if pid names a live process. Unlike status.live_owner, this pid is alive."""
    if pid is None or pid == "":
        return False
    try:
        pid_i = int(pid)
    except (TypeError, ValueError, OverflowError):
        return False
    if isinstance(pid, bool) or pid_i <= 0:
        return False
    me = os.getpid() if self_pid is None else int(self_pid)
    if pid_i == me:
        return True
    try:
        os.kill(pid_i, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def load_bag(home: Path) -> dict[str, Any]:
    path = Path(home) / BAG_REL
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    for key in ("targets", "skipped"):
        if key in data:
            data[key] = [row for row in data[key] if isinstance(row, dict)] if isinstance(data[key], list) else []
    return data


def _write_bag(home: Path, data: dict[str, Any]) -> Path:
    path = Path(home) / BAG_REL
    payload = dict(data)
    payload.setdefault("schema", BAG_SCHEMA)
    atomic_write_json(path, payload)
    return path


def save_bag(home: Path, data: dict[str, Any]) -> Path:
    return with_home_lock(home, "bag", lambda: _write_bag(home, data))


def load_merged_status(home: Path) -> dict[str, Any]:
    snap = StatusBoard(home).snapshot()
    snap["bag"] = load_bag(home)
    return snap


def recover_stale_bag(home: Path, *, locked: bool = False) -> None:
    """Mark a running bag halted when its pid is dead. This process is never stale."""
    if not locked:
        return with_home_lock(home, "bag", lambda: recover_stale_bag(home, locked=True))
    bag = load_bag(home)
    if bag.get("state") != "running":
        return
    if pid_alive(bag.get("runner_pid")):
        return
    bag["state"] = "halted"
    bag["halt_reason"] = "interrupted"
    bag["runner_pid"] = None
    for t in bag.get("targets") or []:
        if isinstance(t, dict) and t.get("state") in {"queued", "running"}:
            t["state"] = "skipped"
            t["error"] = "interrupted"
    _write_bag(home, bag)


def assert_shift_idle(
    home: Path,
    *,
    self_pid: int | None = None,
    allow_self: bool = False,
) -> None:
    """Raise SafetyError if a bag or night is live.

    allow_self=True only for run_bag → run_night (same pid holds the bag lock).
    start_run / POST /api/bag / cmd_run / run_bag acquire pass allow_self=False.
    """
    me = os.getpid() if self_pid is None else int(self_pid)
    bag = load_bag(home)
    if bag.get("state") == "running" and pid_alive(bag.get("runner_pid"), self_pid=me):
        pid = bag.get("runner_pid")
        try:
            same = int(pid) == me
        except (TypeError, ValueError):
            same = False
        if allow_self and same:
            return
        raise SafetyError("a bag is already running")
    board = StatusBoard(home).read()
    if board.state == "running" and pid_alive(board.runner_pid, self_pid=me):
        if allow_self and board.runner_pid == me:
            return
        raise SafetyError("a shift is already running")


def assert_run_night_idle(home: Path, *, allow_self_bag: bool = False) -> None:
    """Inner run_night gate (N2). A night pre-marked by this pid is always allowed."""
    recover_stale_bag(home)
    me = os.getpid()
    bag = load_bag(home)
    if bag.get("state") == "running" and pid_alive(bag.get("runner_pid"), self_pid=me):
        pid = bag.get("runner_pid")
        try:
            same = int(pid) == me
        except (TypeError, ValueError):
            same = False
        if not (allow_self_bag and same):
            raise SafetyError("a bag is already running")
    board = StatusBoard(home).read()
    if board.state == "running" and pid_alive(board.runner_pid, self_pid=me):
        if board.runner_pid == me:
            return
        raise SafetyError("a shift is already running")


def acquire_bag(home: Path, payload: dict[str, Any], *, self_pid: int | None = None) -> None:
    me = os.getpid() if self_pid is None else int(self_pid)

    def _go() -> None:
        recover_stale_bag(home, locked=True)
        current = load_bag(home)
        if current.get("state") == "running" and pid_alive(
            current.get("runner_pid"), self_pid=me
        ):
            try:
                same = int(current.get("runner_pid")) == me
            except (TypeError, ValueError):
                same = False
            if same:
                if (
                    current.get("bag_id") == payload.get("bag_id")
                    and payload.get("bag_id")
                    and "current_index" not in current
                ):
                    # The deck reserves a bag before its worker starts. Keep a
                    # halt request that arrived during that handoff.
                    _write_bag(home, {**payload, "halt_bag": bool(current.get("halt_bag") or payload.get("halt_bag"))})
                    return
            raise SafetyError("a bag is already running")
        assert_shift_idle(home, self_pid=me, allow_self=False)
        _write_bag(home, payload)

    with_home_lock(home, "bag", _go)


def load_prior(home: Path) -> dict[str, list[str]]:
    path = Path(home) / PRIOR_REL
    empty: dict[str, list[str]] = {"liked": [], "skip": []}
    if not path.is_file():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return empty
    if not isinstance(data, dict):
        return empty

    def _names(raw: Any) -> list[str]:
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(x).strip() for x in raw if str(x).strip()]

    return {"liked": _names(data.get("liked")), "skip": _names(data.get("skip"))}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_bag_id(now: datetime) -> str:
    return f"b-{now.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"


def _skip_reason(path: Path, *, allow_dirty: bool, skip_names: set[str]) -> str:
    name = path.name
    if name in skip_names:
        return "prior skip"
    try:
        branch = current_branch(path)
    except Exception:
        return "not a git work tree"
    if branch.startswith("night/"):
        return "on night branch"
    ts = tree_state(path)
    if ts.in_progress:
        return "in progress"
    if ts.detached:
        return "detached HEAD"
    if ts.dirty and not allow_dirty:
        return "dirty tree"
    return ""


def _age_days(last_ct: int, now: datetime) -> float:
    if not last_ct:
        return 10_000.0
    try:
        return max(0.0, (now.timestamp() - int(last_ct)) / 86400.0)
    except (TypeError, ValueError, OSError):
        return 10_000.0


def _score_key(target: BagTarget, *, now: datetime, liked: set[str]) -> tuple:
    hole = HOLE_SCORE.get(int(target.cmm_level), 0)
    age = _age_days(target.last_commit_unix, now)
    recency = 2 if age <= 7 else (1 if age <= 30 else 0)
    liked_n = 1 if target.name in liked else 0
    return (hole, recency, liked_n, -age)


def _make_target(
    path: Path,
    *,
    forum: dict[str, Any],
    home: Path,
    role: str,
    skip_reason: str = "",
) -> BagTarget:
    resolved = resolve_repo(path)
    scored = score_repo(resolved, forum, home=home) if not skip_reason else {}
    return BagTarget(
        path=resolved,
        name=resolved.name,
        repo_id=repo_id(resolved),
        role=role,
        cmm_level=int(scored.get("level") or 0),
        last_commit_unix=last_commit_unix(resolved) if not skip_reason else 0,
        skip_reason=skip_reason,
    )


def _candidate_paths(settings: Settings, *, skip_meta: bool) -> tuple[list[Path], Path | None]:
    found = find_repos(settings.roots, include_deprecated=settings.include_deprecated)
    seen: set[Path] = set()
    others: list[Path] = []
    nightshifts: list[Path] = []
    for entry in found:
        path = Path(entry.path)
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if is_nightshift_repo(resolved):
            nightshifts.append(resolved)
        else:
            others.append(resolved)
    if skip_meta:
        return others, None
    pkg_r: Path | None = None
    pkg = package_checkout()
    if pkg is not None:
        try:
            pkg_r = pkg.resolve()
        except OSError:
            pkg_r = pkg
    meta: Path | None = None
    if pkg_r is not None and any(p == pkg_r for p in nightshifts):
        meta = pkg_r
    elif nightshifts:
        meta = nightshifts[0]
    elif pkg_r is not None and is_nightshift_repo(pkg_r):
        meta = pkg_r
    paths = list(others)
    if meta is not None:
        paths.append(meta)
    return paths, meta


def select_bag(
    settings: Settings,
    *,
    size: int | None = None,
    skip_meta: bool | None = None,
    meta_last: bool = False,
) -> BagPlan:
    home = settings.state_dir()
    recover_stale_bag(home)
    assert_shift_idle(home, allow_self=False)

    size_n = clamp_bag_size(size if size is not None else settings.bag_size)
    skip = settings.skip_meta if skip_meta is None else bool(skip_meta)
    meta_last_b = bool(meta_last or settings.meta_last)
    now = settings.now_fn()
    prior = load_prior(home)
    skip_names = set(prior["skip"])
    liked = set(prior["liked"])
    forum = load_forum(home)
    allow_dirty = bool(settings.allow_dirty)

    paths, meta_path = _candidate_paths(settings, skip_meta=skip)
    if meta_path is None and not skip:
        log("meta Nightshift not in roots; bag has no RSI")

    skipped: list[BagTarget] = []
    eligible: list[BagTarget] = []
    meta_target: BagTarget | None = None
    for path in paths:
        is_meta = bool(meta_path and path.resolve() == meta_path.resolve())
        reason = _skip_reason(path, allow_dirty=allow_dirty, skip_names=skip_names)
        role = "meta" if is_meta else "portfolio"
        if reason:
            row = _make_target(path, forum=forum, home=home, role=role, skip_reason=reason)
            skipped.append(row)
            if is_meta:
                if reason == "dirty tree":
                    log("meta skipped: dirty tree")
                elif reason == "on night branch":
                    log("meta skipped: on night branch")
                else:
                    log(f"meta skipped: {reason}")
            continue
        row = _make_target(path, forum=forum, home=home, role=role)
        if is_meta:
            meta_target = row
        else:
            eligible.append(row)

    eligible.sort(key=lambda t: _score_key(t, now=now, liked=liked), reverse=True)
    targets: list[BagTarget] = []
    if meta_target is not None and not meta_last_b:
        targets.append(meta_target)
        meta_target = None
    slots = size_n - len(targets) - (1 if meta_target is not None else 0)
    targets.extend(eligible[: max(0, slots)])
    if meta_target is not None:
        targets.append(meta_target)
    targets = targets[:size_n]

    plan = BagPlan(
        bag_id=_new_bag_id(now),
        targets=targets,
        skipped=skipped,
        size=size_n,
        skip_meta=skip,
        meta_last=meta_last_b,
    )
    def _save_plan() -> None:
        # Selection may take seconds; another process can start meanwhile.
        assert_shift_idle(home, allow_self=False)
        _write_bag(home, _bag_document(plan, settings, state="idle", runner_pid=None, now=now))

    with_home_lock(home, "bag", _save_plan)
    return plan


def target_to_dict(target: BagTarget, *, state: str = "queued") -> dict[str, Any]:
    st = "skipped" if target.skip_reason else state
    return {
        "repo_id": target.repo_id,
        "name": target.name,
        "path": str(target.path),
        "role": target.role,
        "cmm_level": target.cmm_level,
        "state": st,
        "branch": "",
        "halt_reason": "",
        "remaining_count": None,
        "error": target.skip_reason,
        "landed": None,
        "voided": None,
    }


def plan_to_dict(plan: BagPlan) -> dict[str, Any]:
    return {
        "schema": BAG_SCHEMA,
        "bag_id": plan.bag_id,
        "size": plan.size,
        "skip_meta": plan.skip_meta,
        "meta_last": plan.meta_last,
        "targets": [target_to_dict(t) for t in plan.targets],
        "skipped": [target_to_dict(t) for t in plan.skipped],
    }


def _bag_document(
    plan: BagPlan,
    settings: Settings,
    *,
    state: str,
    runner_pid: int | None,
    now: datetime,
    halt_bag: bool = False,
    deadline: float = 0.0,
) -> dict[str, Any]:
    return {
        "schema": BAG_SCHEMA,
        "bag_id": plan.bag_id,
        "state": state,
        "halt_bag": halt_bag,
        "runner_pid": runner_pid,
        "started_at": _iso(now),
        "halt_at": settings.halt_at,
        "deadline": deadline,
        "size": plan.size,
        "skip_meta": plan.skip_meta,
        "meta_last": plan.meta_last,
        "mock": bool(settings.mock),
        "brief_size": int(settings.brief_size),
        "current_index": -1,
        "targets": [target_to_dict(t) for t in plan.targets],
    }


def _iso(now: datetime) -> str:
    try:
        return now.isoformat(timespec="seconds")
    except (AttributeError, TypeError):
        return _utc_now()


def render_bag_table(plan: BagPlan) -> str:
    lines = [
        f"bag\t{plan.bag_id}\tsize {plan.size}\t{len(plan.targets)} targets"
    ]
    for t in plan.targets:
        lines.append(f"{t.role}\tL{t.cmm_level}\t{t.name}\t{t.path}")
    for t in plan.skipped:
        lines.append(f"skip\t{t.skip_reason}\t{t.name}\t{t.path}")
    return "\n".join(lines)


def mutate_bag(home: Path, fn: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Read, mutate, and replace a bag under one lock so progress and halts coexist."""
    def _go() -> dict[str, Any]:
        bag = load_bag(home)
        fn(bag)
        _write_bag(home, bag)
        return bag

    return with_home_lock(home, "bag", _go)


_patch_bag = mutate_bag


def _skip_rest(bag: dict[str, Any], reason: str, error: str = "") -> None:
    for t in bag.get("targets") or []:
        if isinstance(t, dict) and t.get("state") in {"queued", "running"}:
            t["state"] = "skipped"
            t["halt_reason"] = reason
            t["error"] = error or reason


def _safe_stub(home: Path, repo: Path, error: str, *, mock: bool, bag_id: str) -> None:
    if not forum_enabled():
        return
    try:
        publish_error_stub(home=home, repo=repo, error=error, mock=mock, bag_id=bag_id)
    except Exception as exc:
        log(f"forum error stub failed: {exc}")


def _queued_count(bag: dict[str, Any]) -> int:
    return sum(
        1
        for t in bag.get("targets") or []
        if isinstance(t, dict) and t.get("state") in {"queued", "running"}
    )


def run_bag(plan: BagPlan, settings: Settings) -> dict[str, Any]:
    from .graph import next_halt
    from .runner import run_night

    home = settings.state_dir()
    now = settings.now_fn()
    deadline = settings.halt_deadline or next_halt(settings.halt_at, now)
    night_settings = replace(settings, halt_deadline=deadline, bag_id=plan.bag_id)
    min_minutes = int(getattr(settings, "bag_min_minutes", BAG_MIN_MINUTES) or 0)
    interrupted = False
    crashed = False
    main_touched = False
    started_observe = False
    acquire_bag(
        home,
        _bag_document(
            plan,
            night_settings,
            state="running",
            runner_pid=os.getpid(),
            now=now,
            halt_bag=False,
            deadline=deadline.timestamp(),
        ),
    )
    try:
        try:
            if settings.observe:
                started_observe = True
                observe_start(
                    open_browser=settings.open_browser,
                    jsonl=str(home / "bag-events.jsonl"),
                    port=settings.loopscope_port,
                    host="127.0.0.1",
                )
                # Inner nights keep their Ralph+graph on this bus.
                night_settings = replace(night_settings, observe=False)
            n = len(plan.targets)
            loop = ralph_loop(
                "tonight's bag remaining",
                phases=["select", "night", "forum"],
                max_iters=max(n, 1),
                stall_after=0,
                roles={
                    "select": "next target from the frozen bag",
                    "night": "run_night on this target",
                    "forum": "publish after halt (already done inside run_night)",
                },
            ) if n else iter(())
            for it in loop:
                i = int(getattr(it, "n", getattr(it, "index", 0)) or 0) - 1
                if i < 0:
                    i = 0
                if i >= n:
                    it.done("bag_empty")
                    break
                target = plan.targets[i]
                bag = load_bag(home)
                skip_reason = ""
                if bag.get("halt_bag"):
                    skip_reason = "bag_halted"
                else:
                    clock = night_settings.now_fn()
                    if clock >= deadline:
                        skip_reason = "clock"
                    else:
                        remaining_min = (deadline - clock).total_seconds() / 60
                        if min_minutes and remaining_min < min_minutes:
                            skip_reason = "clock_short"

                with it.phase("select") as phase:
                    phase.log(
                        f"{target.role} {target.name} L{target.cmm_level} {target.path}"
                    )
                    if skip_reason:
                        phase.log(f"skip {skip_reason}", level="warn")

                if skip_reason:
                    _patch_bag(home, lambda b, r=skip_reason: _skip_rest(b, r))
                    it.signal(0, name="bag_remaining")
                    it.note(skip_reason)
                    it.done(skip_reason)
                    break

                def _mark_running(b: dict[str, Any], idx: int = i) -> None:
                    b["current_index"] = idx
                    targets = b.get("targets") or []
                    if idx < len(targets) and isinstance(targets[idx], dict):
                        targets[idx]["state"] = "running"

                _patch_bag(home, _mark_running)
                report = None
                try:
                    with it.phase("night") as phase:
                        phase.log(f"run_night {target.name}")
                        try:
                            report = run_night(
                                target.path,
                                night_settings,
                                explicit=(target.role == "meta"),
                                allow_self_bag=True,
                            )
                        except KeyboardInterrupt:
                            interrupted = True

                            def _halt(b: dict[str, Any]) -> None:
                                b["halt_bag"] = True
                                _skip_rest(b, "interrupted")

                            _patch_bag(home, _halt)
                            it.done("interrupted")
                            raise
                        except Exception as exc:
                            if not getattr(exc, "nightshift_forum_published", False):
                                _safe_stub(
                                    home,
                                    target.path,
                                    str(exc),
                                    mock=bool(night_settings.mock),
                                    bag_id=plan.bag_id,
                                )
                                try:
                                    setattr(exc, "nightshift_forum_published", True)
                                except Exception:
                                    pass

                            def _err(
                                b: dict[str, Any], idx: int = i, err: str = str(exc)
                            ) -> None:
                                targets = b.get("targets") or []
                                if idx < len(targets) and isinstance(targets[idx], dict):
                                    targets[idx]["state"] = "error"
                                    targets[idx]["error"] = err
                                    targets[idx]["halt_reason"] = "error"

                            _patch_bag(home, _err)
                            phase.log(str(exc), level="warn")
                except KeyboardInterrupt:
                    interrupted = True
                    raise

                with it.phase("forum") as phase:
                    if report is None:
                        phase.log("error stub (no NightReport)")
                    else:
                        phase.log(
                            f"published {target.name} {report.halt_reason} "
                            f"remaining {report.remaining_count}"
                        )

                if report is not None:
                    if not report.main_untouched:
                        main_touched = True

                    def _ok(b: dict[str, Any], idx: int = i) -> None:
                        targets = b.get("targets") or []
                        if idx < len(targets) and isinstance(targets[idx], dict):
                            row = targets[idx]
                            row["state"] = "done"
                            row["branch"] = report.branch
                            row["halt_reason"] = report.halt_reason
                            row["remaining_count"] = report.remaining_count
                            row["error"] = report.error
                            row["landed"] = report.landed
                            row["voided"] = report.voided

                    _patch_bag(home, _ok)

                remaining = _queued_count(load_bag(home))
                it.signal(remaining, name="bag_remaining")
                it.note(
                    f"{target.name} "
                    + (
                        (report.halt_reason if report is not None else "error")
                    )
                )
                if remaining == 0 or i >= n - 1:
                    it.done("bag_done")
                    break
        except KeyboardInterrupt:
            interrupted = True
            raise
        except Exception:
            crashed = True
            raise
    finally:
        def _finish(bag: dict[str, Any]) -> None:
            if bag.get("state") != "running" or bag.get("bag_id") != plan.bag_id:
                return
            if interrupted or bag.get("halt_bag"):
                bag["state"] = "halted"
                _skip_rest(bag, "interrupted" if interrupted else "bag_halted")
            elif crashed:
                bag["state"] = "error"
                bag["halt_reason"] = "error"
                _skip_rest(bag, "error")
            else:
                bag["state"] = "done"
            bag["runner_pid"] = None
        try:
            mutate_bag(home, _finish)
        finally:
            if started_observe:
                stop_active()
    final = load_bag(home)
    return {
        "bag_id": plan.bag_id,
        "state": final.get("state") or "",
        "targets": final.get("targets") or [],
        "main_touched": main_touched,
        "interrupted": interrupted,
    }


def bag_exit_code(result: dict[str, Any]) -> int:
    if result.get("interrupted"):
        return 130
    if result.get("main_touched"):
        return 3
    targets = [t for t in (result.get("targets") or []) if isinstance(t, dict)]
    if not targets:
        return 1
    if any(t.get("state") == "error" for t in targets):
        return 2
    if any(t.get("state") == "skipped" for t in targets):
        return 2
    if any(int(t.get("remaining_count") or 0) for t in targets if t.get("state") == "done"):
        return 2
    return 0
