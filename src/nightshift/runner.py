"""Overnight contract: freeze a brief, Ralph until remaining_count hits 0."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import forum
from .config import Settings
from .gitops import (
    checkout_night_branch,
    commit_paths,
    commits_since,
    current_branch,
    default_branch,
    git,
    night_branch_name,
    list_local_branches,
    push_branch,
    rev_parse,
)
from .graph import (
    LoopNodes,
    NightContext,
    NightState,
    build_cycle_app,
    next_halt,
    parse_halt_at,
    read_snapshot,
    run_cycle,
)
from .host import count_failed as host_count_failed
from .host import resolve_interpreter
from .ledger import (
    history_void_reason,
    load_ledger,
    merge_night_into_ledger,
    save_ledger,
)
from .llm import Critic, MockChatClient, OpenAICompatClient, Writer, persist_meta, probe_models
from .models import Brief, SafetyError
from .observe import attach, finish, log, metric, ralph_loop, start as observe_start, stop_active
from .safety import assert_clean_tree, assert_safe_target
from .status import StatusBoard, clear_halt, halt_requested
from .summary import date_from_branch, halt_words, night_view, render_markdown, write_summary_file


@dataclass
class NightReport:
    repo: Path
    branch: str
    main_ref: str
    main_sha: str
    remaining_count: int
    halt_reason: str
    brief: Brief | None = None  # None on crash / freeze-fail paths; never Brief.freeze([])
    summary_path: Path | None = None
    refused: list[str] = field(default_factory=list)
    base_ref: str = ""
    base_sha: str = ""
    main_untouched: bool = True
    started_at: str = ""
    ended_at: str = ""
    mock: bool = False
    error: str = ""
    lens_hint: str = ""  # "oe" | "de" | ""
    landed: int = 0
    voided: int = 0


LENS_BLOCK_OE = (
    "## Freeze lens\n"
    "This clone has operational evidence (ledger / last host checks).\n"
    "Prefer at least one upgrade that hardens a previously attempted check if that is still checkable.\n"
    "JOBS N is the total bag. Do not emit DE and OE as separate tracks.\n"
)


def freeze_lens_hint(repo: Path, home: Path) -> str:
    """"oe" when this clone's ledger (clone + home shard) holds attempted or done rows, else "de"."""
    ledger = load_ledger(repo, home=home)
    entries = [e for e in ledger.get("entries") or [] if isinstance(e, dict)]
    if any(e.get("attempted") or e.get("done") for e in entries):
        return "oe"
    return "de"


def freeze_snapshot(ctx: NightContext) -> str:
    """Minute-0 snapshot (N5): home ledger plus the other-repo forum excerpt.

    `forum=` is freeze-only — None when NIGHTSHIFT_FORUM is off. Writer turns
    (LoopNodes.writer) pass home= only and never see another repo.
    """
    home = ctx.settings.home
    loaded = forum.load_forum(home) if forum.forum_enabled() else None
    return read_snapshot(ctx.repo, home=home, forum=loaded)


def _mark_published(exc: BaseException | None) -> None:
    """N3: the forum already holds this failure; a bag must not stub it again."""
    if exc is None:
        return
    try:
        setattr(exc, "nightshift_forum_published", True)
    except Exception:
        pass


def _safe_publish(
    ctx: NightContext,
    report: NightReport,
    ledger: dict[str, Any],
    exc: BaseException | None = None,
) -> None:
    """Paths (1)-(2). Never fails the night: a publish error is one log line.

    `exc` is the exception this row records (crash, failed tail); it is marked
    only when the publish succeeded, like `_safe_publish_error`.
    """
    if not forum.forum_enabled():
        return
    try:
        forum.publish_night(home=ctx.settings.home, report=report, ledger=ledger)
    except Exception as err:
        log(f"forum publish failed: {err}")
        return
    _mark_published(exc)


def _publish_failed_night(
    ctx: NightContext,
    *,
    exc: BaseException,
    branch: str,
    state: NightState,
    brief: Brief | None,
    ledger: dict[str, Any] | None,
    started_at: str,
) -> None:
    """A night that raised after its branch exists: path (2), or a failed tail.

    The row is `halt_reason="error"` with the exception text, the brief from
    state when there is one, the summary if its file exists, and the ledger
    just committed when that ran — else the rows already on disk. Never an
    `error/<ts>` stub: this night has a branch.
    """
    summary_file = ctx.repo / ".nightshift" / "summary.md"
    try:
        main_untouched = rev_parse(ctx.repo, ctx.main_ref) == ctx.main_sha
    except Exception:
        main_untouched = True
    landed, voided, _ = forum.brief_counts(brief)
    _safe_publish(
        ctx,
        NightReport(
            repo=ctx.repo,
            branch=branch,
            main_ref=ctx.main_ref,
            main_sha=ctx.main_sha,
            remaining_count=int(state.get("remaining_count") or 0),
            halt_reason="error",
            brief=brief,
            summary_path=summary_file if summary_file.is_file() else None,
            refused=list(ctx.refused),
            base_ref=ctx.base_ref,
            base_sha=ctx.base_sha,
            main_untouched=main_untouched,
            started_at=started_at,
            ended_at=_iso_seconds(ctx.clock()),
            mock=bool(ctx.settings.mock),
            error=str(exc),
            lens_hint=ctx.lens_hint,
            landed=landed,
            voided=voided,
        ),
        ledger if ledger is not None else load_ledger(ctx.repo, home=ctx.settings.home),
        exc=exc,
    )


def _safe_publish_error(
    ctx: NightContext,
    error: str,
    exc: BaseException | None = None,
    *,
    started_at: str = "",
) -> None:
    """Path (3): freeze failed on base. Stub only; marks `exc` so a bag does not stub twice (N3)."""
    if not forum.forum_enabled():
        return
    try:
        forum.publish_error_stub(
            home=ctx.settings.home,
            repo=ctx.repo,
            error=error,
            mock=bool(ctx.settings.mock),
            started_at=started_at,
            bag_id=str(getattr(ctx.settings, "bag_id", "") or ""),
        )
    except Exception as err:
        log(f"forum error stub failed: {err}")
        return
    _mark_published(exc)


def make_clients(settings: Settings, repo: Path):
    if settings.mock:
        writer_client = MockChatClient("writer", repo)
        critic_client = MockChatClient("critic", repo)
        return writer_client, critic_client
    writer_client = OpenAICompatClient(
        settings.writer_base_url,
        settings.writer_model,
        api_key=settings.api_key,
        timeout=settings.writer_timeout,
    )
    critic_client = OpenAICompatClient(
        settings.critic_base_url,
        settings.critic_model,
        api_key=settings.api_key,
        timeout=settings.critic_timeout,
    )
    return writer_client, critic_client


def probe_brains(settings: Settings) -> None:
    if settings.mock:
        return
    from .observe import log as _log

    for role, url, model in (
        ("critic", settings.critic_base_url, settings.critic_model),
        ("writer", settings.writer_base_url, settings.writer_model),
    ):
        try:
            body = probe_models(url, settings.api_key, timeout=5)
        except RuntimeError as exc:
            raise SafetyError(f"{role} unreachable at {url}: {exc}") from exc
        ids = []
        for row in body.get("data") or []:
            if isinstance(row, dict) and row.get("id"):
                ids.append(str(row["id"]))
        if body.get("missing_ok"):
            _log(f"{role} /models 404 at {url}; continuing")
            continue
        if ids and model not in ids:
            _log(f"{role} model {model} not in /models at {url}; continuing")


def write_summary(
    ctx: NightContext,
    brief: Brief,
    halt_reason: str,
    *,
    error: str = "",
    extra_header: list[str] | None = None,
) -> Path:
    repo = ctx.repo
    branch = ctx.status.current.branch or brief.branch
    persist_meta(
        repo,
        ".nightshift/brief.json",
        json.dumps(brief.to_dict(), indent=2) + "\n",
    )
    date = date_from_branch(branch, ctx.clock().strftime("%Y-%m-%d"))
    try:
        view = night_view(repo, branch=branch)
    except Exception:
        view = {
            "jobs": [
                {
                    "id": u.id,
                    "title": u.title,
                    "state": "void" if u.void else ("done" if u.done else "open"),
                    "void_reason": u.void_reason,
                    "note": u.note,
                    "check": u.check_command,
                    "paths": list(u.paths),
                    "turns": 0,
                    "commits": [],
                    "fingerprint": "",
                    "tail": "",
                }
                for u in brief.upgrades
            ],
            "landed": sum(1 for u in brief.upgrades if u.done),
            "voided": sum(1 for u in brief.upgrades if u.void),
            "remaining": brief.remaining_count,
            "branch": branch,
            "base": {"ref": ctx.base_ref or ctx.main_ref, "sha": ctx.base_sha or ctx.main_sha},
            "main": {"ref": ctx.main_ref, "sha": ctx.main_sha},
            "halt_reason": halt_reason,
            "halt_words": halt_words(
                halt_reason, remaining=brief.remaining_count, turn=ctx.status.current.turn
            ),
            "commits": [],
            "changed_stat": "",
            "land": {
                "merge": f"git checkout {ctx.base_ref or ctx.main_ref} && git merge --no-ff {branch}",
                "cherry_pick": "",
                "drop": f"git branch -D {branch}",
            },
            "refused_by_turn": {},
            "review_cmd": "",
        }
    view["halt_reason"] = halt_reason
    view["halt_words"] = halt_words(
        halt_reason,
        remaining=brief.remaining_count,
        turn=ctx.status.current.turn,
    )
    if error:
        view["halt_words"] = f"crashed at turn {ctx.status.current.turn}: {error}"
    header: list[str] = []
    tail_sections: list[str] = []
    for line in extra_header or []:
        if str(line).lstrip().startswith("## "):
            tail_sections.append(str(line))
        else:
            header.append(str(line))
    if ctx.interpreter:
        header.append(
            f"**Host python:** {ctx.interpreter}"
            + (f" ({ctx.interpreter_source})" if ctx.interpreter_source else "")
        )
    if ctx.settings.allow_dirty and ctx.preexisting:
        header.append(f"**Pre-existing dirt kept out:** {len(ctx.preexisting)} paths")
    text = render_markdown(
        view,
        date=date,
        repo=repo,
        extra_header=header,
        refused_fallback=list(ctx.refused),
    )
    if error:
        text = text.rstrip() + f"\n\n## Error\n\n{error}\n"
    for section in tail_sections:
        text = text.rstrip() + "\n\n" + section.rstrip() + "\n"
    path = write_summary_file(repo, text)
    commit_paths(
        repo,
        "nightshift: morning summary",
        [".nightshift/summary.md"],
        exclude=ctx.preexisting,
    )
    return path


def freeze_brief(ctx: NightContext, snapshot: str, branch_name: str) -> Brief:
    """Critic propose + ledger/dirty void. No writes.

    The OE lens block (§8) is prepended to the snapshot when this clone has
    operational evidence; the hint is stored once on ctx.lens_hint, never on
    turn_scratch. JOBS N stays the total bag.
    """
    size = int(ctx.settings.brief_size)
    ctx.lens_hint = freeze_lens_hint(ctx.repo, ctx.settings.home)
    if ctx.lens_hint == "oe":
        snapshot = LENS_BLOCK_OE + "\n" + snapshot
    proposed = ctx.critic.propose_brief(snapshot, size=size)
    brief = Brief.freeze(
        list(proposed) if not isinstance(proposed, tuple) else list(proposed),
        repo=str(ctx.repo),
        branch=branch_name,
        base_ref=ctx.base_ref,
        base_sha=ctx.base_sha,
    )
    ledger = load_ledger(ctx.repo, home=ctx.settings.home)
    for upgrade in list(brief.upgrades):
        if upgrade.void:
            continue
        reason = history_void_reason(upgrade, ledger)
        if reason:
            brief.void_upgrade(upgrade.id, reason)
    if ctx.preexisting:
        pre = {p.replace("\\", "/") for p in ctx.preexisting}
        for upgrade in list(brief.upgrades):
            if upgrade.void:
                continue
            paths = {p.replace("\\", "/") for p in upgrade.paths if str(p).strip()}
            if paths & pre:
                brief.void_upgrade(upgrade.id, "dirty_in_tree")
    return brief


def persist_brief(ctx: NightContext, brief: Brief) -> NightState:
    persist_meta(
        ctx.repo,
        ".nightshift/brief.json",
        json.dumps(brief.to_dict(), indent=2) + "\n",
    )
    turns_path = ctx.repo / ".nightshift" / "turns.jsonl"
    turns_path.parent.mkdir(parents=True, exist_ok=True)
    freeze_row = {
        "turn": 0,
        "kind": "freeze",
        "brief": brief.to_dict(),
        "void_ids": [u.id for u in brief.upgrades if u.void],
        "secs": {"critic": 0},
    }
    turns_path.write_text(json.dumps(freeze_row) + "\n", encoding="utf-8")
    commit_paths(
        ctx.repo,
        f"nightshift: freeze brief ({len(brief.upgrades)} upgrades)",
        [".nightshift/brief.json", ".nightshift/turns.jsonl"],
        exclude=ctx.preexisting,
    )
    log(f"frozen brief: {len(brief.upgrades)} upgrades")
    metric("remaining_count", float(brief.remaining_count))
    ctx.status.update(
        remaining_count=brief.remaining_count,
        brain="critic",
        brief=brief.to_dict(),
        turns=[freeze_row],
    )
    return {
        "repo": str(ctx.repo),
        "branch": brief.branch,
        "brief": brief.to_dict(),
        "remaining_count": brief.remaining_count,
        "job": "",
        "job_upgrade_id": 0,
        "turn": 0,
        "last_check": {},
        "last_diff": "",
        "refused": [],
        "written": [],
        "halt_reason": "",
        "brain": "critic",
        "check_logs": "",
        "check_results": [],
        "main_ref": ctx.main_ref,
        "main_sha": ctx.main_sha,
        "base_ref": ctx.base_ref,
        "base_sha": ctx.base_sha,
        "fail_streak": {},
        "job_feedback": {},
        "turn_refused": [],
        "turns_on_job": {},
        "compile_errors": [],
        "job_red_ids": {},
        "job_base": {},
        "checks": {},
    }


def minute_zero(ctx: NightContext) -> NightState:
    """Back-compat wrapper: snapshot + freeze + persist on the current branch."""
    ctx.status.update(brain="critic", state="running")
    snapshot = freeze_snapshot(ctx)
    branch = ctx.status.current.branch
    brief = freeze_brief(ctx, snapshot, branch)
    return persist_brief(ctx, brief)


def dry_run_brief(repo: Path, settings: Settings, *, explicit: bool = True) -> Brief:
    target = assert_safe_target(repo, explicit=explicit)
    parse_halt_at(settings.halt_at)
    ts = assert_clean_tree(target, allow_dirty=settings.allow_dirty)
    probe_brains(settings)
    writer_client, critic_client = make_clients(settings, target)
    board = StatusBoard(settings.state_dir())
    clock = settings.now_fn
    deadline = settings.halt_deadline or next_halt(settings.halt_at, clock())
    interp = resolve_interpreter(target)
    ctx = NightContext(
        repo=target,
        settings=settings,
        writer=Writer(writer_client, target),
        critic=Critic(critic_client, target),
        status=board,
        clock=clock,
        deadline=deadline,
        explicit=explicit,
        main_ref=default_branch(target),
        main_sha=rev_parse(target, "HEAD"),
        base_ref=current_branch(target),
        base_sha=rev_parse(target, "HEAD"),
        preexisting=set(ts.dirty),
        interpreter=interp.path,
        interpreter_source=interp.source,
    )
    snapshot = freeze_snapshot(ctx)
    return freeze_brief(ctx, snapshot, branch_name="")


def _open_work(state: NightState) -> int:
    remaining = int(state.get("remaining_count") or 0)
    brief = Brief.from_dict(state.get("brief") or {"upgrades": []}) if state.get("brief") else None
    by_id = {int(r["upgrade_id"]): r for r in (state.get("check_results") or [])}
    extra = 0
    if brief is not None:
        for upgrade in brief.remaining():
            row = by_id.get(upgrade.id)
            if row and row.get("ok"):
                extra += 0
            else:
                n = host_count_failed(str((row or {}).get("output") or ""))
                extra += n or 999
    return 1000 * remaining + min(999, extra)


def _commit_ledger(
    ctx: NightContext,
    brief: Brief,
    branch: str,
    state: NightState,
) -> dict[str, Any]:
    """Merge tonight into clone + home ledgers, commit, and return the merged ledger."""
    attempted_ids = {int(r["upgrade_id"]) for r in (state.get("check_results") or [])}
    last_exit = {
        int(r["upgrade_id"]): int(r.get("exit_code") or 0)
        for r in (state.get("check_results") or [])
    }
    turns_on_job = dict(state.get("turns_on_job") or {})
    turns_by_id = {
        u.id: int(turns_on_job.get(str(u.id), 0) or 0) for u in brief.upgrades
    }
    ledger = merge_night_into_ledger(
        load_ledger(ctx.repo, home=ctx.settings.home),
        brief,
        branch,
        turns_by_id=turns_by_id,
        attempted_ids=attempted_ids,
        last_exit_by_id=last_exit,
    )
    save_ledger(ctx.repo, ledger, home=ctx.settings.home)
    commit_paths(
        ctx.repo,
        "nightshift: update ledger",
        [".nightshift/ledger.json", ".nightshift/turns.jsonl"],
        exclude=ctx.preexisting,
    )
    return ledger


def _iso_seconds(value: Any) -> str:
    try:
        return value.isoformat(timespec="seconds")
    except (AttributeError, TypeError):
        return str(value or "")


def run_night(
    repo: Path,
    settings: Settings,
    *,
    explicit: bool = True,
    allow_self_bag: bool = False,
) -> NightReport:
    target = assert_safe_target(repo, explicit=explicit)
    from .bag import assert_run_night_idle

    assert_run_night_idle(settings.state_dir(), allow_self_bag=allow_self_bag)
    board = StatusBoard(settings.state_dir())
    clock = settings.now_fn
    parse_halt_at(settings.halt_at)
    deadline = settings.halt_deadline or next_halt(settings.halt_at, clock())
    ts = assert_clean_tree(target, allow_dirty=settings.allow_dirty)
    probe_brains(settings)
    interp = resolve_interpreter(target)
    base_ref = current_branch(target)
    base_sha = rev_parse(target, "HEAD")
    main_ref = default_branch(target)
    main_sha = rev_parse(target, main_ref)
    now = clock()
    started_at = _iso_seconds(now)
    branch = night_branch_name(list_local_branches(target), now)
    board.update(
        state="running",
        runner_pid=os.getpid(),
        repo=str(target),
        brain="critic",
        remaining_count=int(settings.brief_size),
        mock=settings.mock,
        halt_reason="",
        summary="",
        error="",
        branch="",
        loopscope_url=f"http://127.0.0.1:{settings.loopscope_port}",
        started_at=now.timestamp(),
        deadline=deadline.timestamp(),
        max_turns=settings.max_turns,
        host_python=interp.path,
    )
    writer_client, critic_client = make_clients(settings, target)
    ctx = NightContext(
        repo=target,
        settings=settings,
        writer=Writer(writer_client, target),
        critic=Critic(critic_client, target),
        status=board,
        clock=clock,
        deadline=deadline,
        explicit=explicit,
        main_ref=main_ref,
        main_sha=main_sha,
        base_ref=base_ref,
        base_sha=base_sha,
        preexisting=set(ts.dirty),
        interpreter=interp.path,
        interpreter_source=interp.source,
    )
    snapshot = freeze_snapshot(ctx)
    try:
        brief = freeze_brief(ctx, snapshot, branch)
    except Exception as exc:
        board.update(
            state="error",
            runner_pid=None,
            error=str(exc),
            branch="",
            brain="",
        )
        # Path (3): still on base, no branch, no brief, no ledger. Stub only.
        _safe_publish_error(ctx, str(exc), exc, started_at=started_at)
        raise
    checkout_night_branch(target, now, name=branch)
    board.update(branch=branch)
    jsonl = target / ".nightshift" / "events.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    if settings.observe:
        observe_start(
            open_browser=settings.open_browser,
            jsonl=str(jsonl),
            port=settings.loopscope_port,
        )
    try:
        nodes = LoopNodes(ctx)
        app = build_cycle_app(nodes)
        try:
            state = persist_brief(ctx, brief)
        except Exception as exc:
            porcelain = git(target, "status", "--porcelain", check=False).stdout
            dirty = [
                ln
                for ln in porcelain.splitlines()
                if ln.strip()
                and ".nightshift/" not in ln
                and not any(p in ln for p in ctx.preexisting)
            ]
            if not commits_since(target, base_sha) and not dirty:
                git(target, "checkout", base_ref, check=False)
                msg = f"{exc}; returned to {base_ref}; empty branch {branch} left, delete with git branch -d {branch}"
                board.update(state="error", runner_pid=None, error=msg, brain="", branch=branch)
                raise SafetyError(msg) from exc
            board.update(state="error", runner_pid=None, error=str(exc), brain="")
            raise
        halt_reason = ""
        loop = ralph_loop(
            "overnight remaining_count",
            phases=["critic_job", "writer", "host_check", "critic_score"],
            max_iters=settings.max_turns,
            stall_after=settings.stall_after,
            roles={
                "critic_job": "one-line job from the frozen brief",
                "writer": "edit files on the night branch",
                "host_check": "run the real check commands",
                "critic_score": "score, slash, revert gold-plating",
            },
        )
        config: dict[str, Any] = {}
        used_ralph_attach = False
        if app is not None and hasattr(loop, "attach_graph"):
            config = loop.attach_graph(app)
            used_ralph_attach = True
        elif app is not None:
            config = attach(
                app,
                roles={
                    "critic_job": "one-line job from the frozen brief",
                    "writer": "edit files on the night branch",
                    "host_check": "run the real check commands",
                    "critic_score": "score, slash, revert gold-plating",
                },
            )
        try:
            for it in loop:
                n = int(getattr(it, "n", getattr(it, "index", 0)) or 0)
                if halt_requested(settings.state_dir(), os.getpid()):
                    halt_reason = "requested"
                    it.done("halt requested")
                    break
                if clock() >= deadline:
                    halt_reason = "clock"
                    it.done("clock halt")
                    break
                if int(state.get("remaining_count") or 0) == 0:
                    halt_reason = "remaining_zero"
                    it.done("remaining_count 0")
                    break
                if state.get("halt_reason"):
                    halt_reason = str(state["halt_reason"])
                    it.done(halt_reason)
                    break
                ctx.status.update(turn=n, brain="critic")
                if app is not None:
                    state = app.invoke(state, config=config)
                else:
                    state = run_cycle(nodes, state)
                remaining = int(state.get("remaining_count") or 0)
                it.signal(remaining, name="remaining_count")
                it.signal(_open_work(state), name="open_work")
                metric("remaining_count", float(remaining))
                log(f"turn {n} remaining_count={remaining}")
                if remaining == 0:
                    halt_reason = "remaining_zero"
                    it.done("remaining_count 0")
                    break
            else:
                if not halt_reason:
                    loop_reason = str(getattr(loop, "reason", "") or "")
                    halt_reason = "stalled" if loop_reason == "stalled" else "max_turns"
        except Exception as exc:
            halt_reason = halt_reason or "error"
            brief_err: Brief | None = None
            ledger_err: dict[str, Any] | None = None
            try:
                if state.get("brief"):
                    brief_err = Brief.from_dict(state["brief"])
                    write_summary(ctx, brief_err, "error", error=str(exc))
                    ledger_err = _commit_ledger(ctx, brief_err, branch, state)
            except Exception:
                pass
            summary_file = ctx.repo / ".nightshift" / "summary.md"
            ctx.status.update(
                state="error",
                runner_pid=None,
                error=str(exc),
                halt_reason=halt_reason,
                summary=summary_file.read_text(encoding="utf-8") if summary_file.is_file() else "",
            )
            if app is not None and not used_ralph_attach:
                finish(config, status="error")
            # Path (2): publish whatever exists — brief from state if any, the
            # ledger just committed if that ran, else the rows already on disk.
            _publish_failed_night(
                ctx,
                exc=exc,
                branch=branch,
                state=state,
                brief=brief_err,
                ledger=ledger_err,
                started_at=started_at,
            )
            raise
        else:
            if app is not None and not used_ralph_attach:
                finish(config, status="ok")
        if not halt_reason:
            if int(state.get("remaining_count") or 0) == 0:
                halt_reason = "remaining_zero"
            else:
                loop_reason = str(getattr(loop, "reason", "") or "")
                halt_reason = "stalled" if loop_reason == "stalled" else "max_turns"
        brief_done: Brief | None = None
        try:
            brief_done = Brief.from_dict(state["brief"])
            extra: list[str] = []
            main_now = rev_parse(target, main_ref)
            main_untouched = main_now == main_sha
            if not main_untouched:
                extra.append(f"## MAIN MOVED\n\nwas {main_sha}\nnow {main_now}")
            summary_path = write_summary(ctx, brief_done, halt_reason, extra_header=extra)
            ledger = _commit_ledger(ctx, brief_done, branch, state)
        except Exception as exc:
            # Ralph finished but the summary or the ledger commit did not. The
            # night still has a branch with turn commits, so it is recorded as a
            # night row (halt_reason error, never an error/<ts> stub), the board
            # leaves "running", and the exception carries the N3 marker.
            summary_file = ctx.repo / ".nightshift" / "summary.md"
            ctx.status.update(
                state="error",
                runner_pid=None,
                error=str(exc),
                halt_reason=halt_reason,
                summary=summary_file.read_text(encoding="utf-8") if summary_file.is_file() else "",
            )
            _publish_failed_night(
                ctx,
                exc=exc,
                branch=branch,
                state=state,
                brief=brief_done,
                ledger=None,
                started_at=started_at,
            )
            raise
        brief = brief_done
        landed, voided, _open = forum.brief_counts(brief)
        report = NightReport(
            repo=target,
            branch=branch,
            main_ref=main_ref,
            main_sha=main_sha,
            remaining_count=brief.remaining_count,
            halt_reason=halt_reason,
            brief=brief,
            summary_path=summary_path,
            refused=list(ctx.refused),
            base_ref=base_ref,
            base_sha=base_sha,
            main_untouched=main_untouched,
            started_at=started_at,
            ended_at=_iso_seconds(clock()),
            mock=bool(settings.mock),
            error="",
            lens_hint=ctx.lens_hint,
            landed=landed,
            voided=voided,
        )
        tail_exc: BaseException | None = None
        try:
            summary_text = summary_path.read_text(encoding="utf-8")
            if settings.push:
                push_branch(target, branch)
            ctx.status.update(
                state="done" if halt_reason == "remaining_zero" else "halted",
                runner_pid=None,
                remaining_count=brief.remaining_count,
                halt_reason=halt_reason,
                summary=summary_text,
                brain="",
                branch=branch,
                main_untouched=main_untouched,
                brief=brief.to_dict(),
                halt_requested=False,
            )
            clear_halt(settings.state_dir())
        except Exception as exc:
            # The night itself completed (ledger committed); the row records what
            # broke after it (push, board) instead of vanishing from the forum.
            report.error = str(exc)
            tail_exc = exc
            raise
        finally:
            # Path (1): after the ledger is committed, exactly once, whether or not
            # the tail above raised. A publish failure never fails the night. The
            # tail's exception, if any, is marked (N3) once its row is in.
            _safe_publish(ctx, report, ledger, exc=tail_exc)
        return report
    finally:
        stop_active()

