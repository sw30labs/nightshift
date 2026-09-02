"""Overnight contract: freeze a brief, Ralph until remaining_count hits 0."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .gitops import (
    checkout_night_branch,
    commit_paths,
    commits_since,
    default_branch,
    diff_stat_against,
    push_branch,
    rev_parse,
)
from .graph import (
    LoopNodes,
    NightContext,
    NightState,
    build_cycle_app,
    next_halt,
    read_snapshot,
    run_cycle,
)
from .llm import Critic, MockChatClient, OpenAICompatClient, Writer, persist_meta
from .models import Brief
from .observe import attach, finish, log, metric, ralph_loop, start as observe_start
from .safety import assert_safe_target
from .status import StatusBoard


@dataclass
class NightReport:
    repo: Path
    branch: str
    main_ref: str
    main_sha: str
    remaining_count: int
    halt_reason: str
    brief: Brief
    summary_path: Path
    refused: list[str]


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


def write_summary(ctx: NightContext, brief: Brief, halt_reason: str) -> Path:
    repo = ctx.repo
    body: list[str] = [
        f"# Nightshift — {ctx.clock().strftime('%Y-%m-%d')}",
        "",
        f"**Repo:** `{repo}`",
        f"**Branch:** `{ctx.status.current.branch}`",
        f"**Halt:** {halt_reason or 'unknown'}",
        f"**Remaining:** {brief.remaining_count}",
        f"**Writer:** `{ctx.settings.writer_model}` @ `{ctx.settings.writer_base_url}`",
        f"**Critic:** `{ctx.settings.critic_model}` @ `{ctx.settings.critic_base_url}`",
        f"**Mock:** {ctx.settings.mock}",
        "",
        "## Frozen brief",
        "",
    ]
    for upgrade in brief.upgrades:
        mark = "done" if upgrade.done else "open"
        body.append(f"- **#{upgrade.id} [{mark}]** {upgrade.title}")
        body.append(f"  - check: `{upgrade.check_command}`")
        body.append(f"  - paths: {', '.join(upgrade.paths) or '(none)'}")
    body.append("")
    body.append("## What changed")
    body.append("")
    stat = diff_stat_against(repo, ctx.main_ref)
    log = commits_since(repo, ctx.main_ref)
    body.append("```")
    body.append(log or "(no commits)")
    body.append("```")
    body.append("")
    body.append("```")
    body.append(stat or "(no diff)")
    body.append("```")
    body.append("")
    body.append("## What the critic refused")
    body.append("")
    if ctx.refused:
        for note in ctx.refused:
            body.append(f"- {note}")
    else:
        body.append("- nothing slashed this night")
    body.append("")
    if brief.remaining_count and halt_reason == "clock":
        body.append("## Remaining (clock halt)")
        body.append("")
        for upgrade in brief.remaining():
            body.append(f"- **#{upgrade.id}** {upgrade.title} — `{upgrade.check_command}`")
        body.append("")
    text = "\n".join(body) + "\n"
    persist_meta(repo, ".nightshift/summary.md", text)
    commit_paths(repo, "nightshift: morning summary", [".nightshift/summary.md"])
    return repo / ".nightshift" / "summary.md"


def minute_zero(ctx: NightContext) -> NightState:
    """Critic only. No writer. Persist a frozen brief on the night branch."""
    ctx.status.update(brain="critic", state="running")
    snapshot = read_snapshot(ctx.repo)
    size = int(ctx.settings.brief_size)
    proposed = ctx.critic.propose_brief(snapshot, size=size)
    branch = ctx.status.current.branch
    brief = Brief.freeze(
        list(proposed) if not isinstance(proposed, tuple) else list(proposed),
        repo=str(ctx.repo),
        branch=branch,
    )
    persist_meta(
        ctx.repo,
        ".nightshift/brief.json",
        json.dumps(brief.to_dict(), indent=2) + "\n",
    )
    commit_paths(
        ctx.repo,
        f"nightshift: freeze brief ({size} upgrades)",
        [".nightshift/brief.json"],
    )
    log(f"frozen brief: {size} upgrades")
    metric("remaining_count", float(brief.remaining_count))
    ctx.status.update(remaining_count=brief.remaining_count, brain="critic")
    return {
        "repo": str(ctx.repo),
        "branch": branch,
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
    }


def run_night(repo: Path, settings: Settings, *, explicit: bool = True) -> NightReport:
    target = assert_safe_target(repo, explicit=explicit)
    board = StatusBoard(settings.state_dir())
    clock = settings.now_fn
    deadline = settings.halt_deadline or next_halt(settings.halt_at, clock())
    main_ref = default_branch(target)
    main_sha = rev_parse(target, main_ref)
    board.update(
        state="running",
        repo=str(target),
        brain="critic",
        remaining_count=int(settings.brief_size),
        mock=settings.mock,
        halt_reason="",
        summary="",
        error="",
        loopscope_url=f"http://127.0.0.1:{settings.loopscope_port}",
    )
    now = clock()
    branch = checkout_night_branch(target, now)
    board.update(branch=branch)
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
    )
    jsonl = target / ".nightshift" / "events.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    scope = None
    if settings.observe:
        scope = observe_start(
            open_browser=settings.open_browser,
            jsonl=str(jsonl),
            port=settings.loopscope_port,
        )
    nodes = LoopNodes(ctx)
    app = build_cycle_app(nodes)
    state = minute_zero(ctx)
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
            metric("remaining_count", float(remaining))
            log(f"turn {n} remaining_count={remaining}")
            if remaining == 0:
                halt_reason = "remaining_zero"
                it.done("remaining_count 0")
                break
        else:
            if not halt_reason:
                halt_reason = "max_turns"
    except Exception as exc:
        halt_reason = halt_reason or "error"
        ctx.status.update(state="error", error=str(exc), halt_reason=halt_reason)
        if app is not None and not used_ralph_attach:
            finish(config, status="error")
        raise
    else:
        if app is not None and not used_ralph_attach:
            finish(config, status="ok")
    if not halt_reason:
        if int(state.get("remaining_count") or 0) == 0:
            halt_reason = "remaining_zero"
        else:
            halt_reason = "max_turns"
    brief = Brief.from_dict(state["brief"])
    summary_path = write_summary(ctx, brief, halt_reason)
    summary_text = summary_path.read_text(encoding="utf-8")
    if settings.push:
        push_branch(target, branch)
    ctx.status.update(
        state="done" if halt_reason == "remaining_zero" else "halted",
        remaining_count=brief.remaining_count,
        halt_reason=halt_reason,
        summary=summary_text,
        brain="",
        branch=branch,
    )
    persist_meta(
        target,
        ".nightshift/status.json",
        json.dumps(ctx.status.snapshot(), indent=2) + "\n",
    )
    # do not hold() — the command deck / CLI should return
    _ = scope
    return NightReport(
        repo=target,
        branch=branch,
        main_ref=main_ref,
        main_sha=main_sha,
        remaining_count=brief.remaining_count,
        halt_reason=halt_reason,
        brief=brief,
        summary_path=summary_path,
        refused=list(ctx.refused),
    )
