"""CLI: list, run, bag, status, serve, morning, turns, halt, forum, cmm. Equally first-class with the command deck."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .bag import (
    assert_shift_idle,
    bag_exit_code,
    load_bag,
    load_merged_status,
    mutate_bag,
    pid_alive,
    recover_stale_bag,
    render_bag_table,
    run_bag,
    select_bag,
)
from .cmm import histogram, population, render_cmm_md, write_cmm
from .config import Settings
from .deck import serve_deck
from .forum import (
    FORUM_REL,
    ingest_forum,
    land_lines,
    load_forum,
    mark_merged,
    render_forum_md,
    select_mark_merged_night,
)
from .gitops import git, rev_parse
from .graph import load_turns
from .host import resolve_interpreter
from .models import SafetyError
from .repos import find_repos
from .runner import dry_run_brief, run_night
from .status import StatusBoard, live_owner, request_halt
from .summary import night_view, render_terminal

_NIGHT_BRANCH = re.compile(r"^night/[0-9A-Za-z._-]+$")


def _settings_from(args: argparse.Namespace, *, mock_default: bool | None = None) -> Settings:
    mock = None
    if getattr(args, "mock", False):
        mock = True
    elif mock_default is not None:
        mock = mock_default
    return Settings.from_cli(
        roots=getattr(args, "roots", None),
        mock=mock,
        push=bool(getattr(args, "push", False)),
        halt_at=getattr(args, "halt_at", None),
        max_turns=getattr(args, "max_turns", None),
        include_deprecated=bool(getattr(args, "include_deprecated", False)),
        observe=not bool(getattr(args, "no_observe", False)),
        host=getattr(args, "host", None),
        port=getattr(args, "port", None),
        brief_size=getattr(args, "brief_size", None),
        allow_dirty=bool(getattr(args, "allow_dirty", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        job_turns=getattr(args, "job_turns", None),
        bag_size=getattr(args, "size", None),
        skip_meta=bool(getattr(args, "skip_meta", False)),
        meta_last=bool(getattr(args, "meta_last", False)),
    )


def _print_dry_run(brief, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(brief.to_dict(), indent=2))
        return 0 if brief.remaining_count else 2
    for upgrade in brief.upgrades:
        if upgrade.void:
            print(f"- [void: {upgrade.void_reason}] #{upgrade.id} {upgrade.title}")
        else:
            print(f"- [ ] #{upgrade.id} {upgrade.title}")
        print(f"  check: {upgrade.check_command}")
        print(f"  paths: {', '.join(upgrade.paths) or '(none)'}")
    return 0 if brief.remaining_count else 2


def cmd_list(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    repos = find_repos(settings.roots, include_deprecated=settings.include_deprecated)
    if not repos:
        roots = " ".join(str(p) for p in settings.roots)
        print(f"no git repos under {roots}", file=sys.stderr)
        return 1
    for repo in repos:
        dirty = " dirty" if repo.dirty else ""
        print(f"{repo.path}\t{repo.branch}{dirty}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    interp = resolve_interpreter(Path(args.repo))
    print(f"python\t{interp.path} ({interp.source})")
    if settings.dry_run:
        brief = dry_run_brief(Path(args.repo), settings, explicit=True)
        return _print_dry_run(brief, as_json=bool(getattr(args, "json", False)))
    recover_stale_bag(settings.state_dir())
    assert_shift_idle(settings.state_dir(), allow_self=False)
    report = run_night(Path(args.repo), settings, explicit=True)
    print(f"branch\t{report.branch}")
    print(f"halt\t{report.halt_reason}")
    print(f"remaining\t{report.remaining_count}")
    print(f"summary\t{report.summary_path}")
    print(f"main\t{report.main_ref} {report.main_sha}")
    print(f"base\t{report.base_ref} {report.base_sha}")
    print(f"main_untouched\t{str(report.main_untouched).lower()}")
    upgrades = report.brief.upgrades if report.brief is not None else ()
    landed = sum(1 for u in upgrades if u.done)
    voided = sum(1 for u in upgrades if u.void)
    print(
        f"verdict\t{landed} of {len(upgrades)} landed · "
        f"{voided} void · {report.remaining_count} open"
    )
    print(f"land\tgit checkout {report.base_ref} && git merge --no-ff {report.branch}")
    print(f"drop\tgit branch -D {report.branch}")
    try:
        still = rev_parse(report.repo, report.main_ref)
        print(f"main_now\t{still}")
    except Exception:
        pass
    if not report.main_untouched:
        return 3
    return 0 if report.halt_reason == "remaining_zero" else 2


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    board = StatusBoard(settings.state_dir())
    snap = board.snapshot()
    if snap.get("state") == "running" and not live_owner(snap.get("runner_pid")):
        # CLI-side reconcile: a dead pid is not a live shift.
        if snap.get("runner_pid") and snap.get("runner_pid") != os.getpid():
            try:
                os.kill(int(snap["runner_pid"]), 0)
            except ProcessLookupError:
                snap = board.update(
                    state="halted",
                    runner_pid=None,
                    brain="",
                    halt_reason="interrupted",
                ).to_dict()
            except (PermissionError, OverflowError, OSError):
                pass
    want_bag = bool(getattr(args, "bag", False))
    if getattr(args, "json", False):
        print(json.dumps(load_merged_status(settings.state_dir()) if want_bag else snap, indent=2))
        return 0
    idle_night = snap.get("state") == "idle" and not snap.get("repo")
    if idle_night and not want_bag:
        print("no overnight run recorded")
        return 1
    if idle_night:
        print("no overnight run recorded")
    else:
        print(f"state     {snap.get('state')}")
        print(f"repo      {snap.get('repo')}")
        print(f"branch    {snap.get('branch')}")
        print(f"brain     {snap.get('brain')}")
        print(f"turn      {snap.get('turn')}")
        print(f"remaining {snap.get('remaining_count')}")
        print(f"halt      {snap.get('halt_reason')}")
        print(f"updated   {snap.get('updated_at')}")
        last = snap.get("last_check") or {}
        if last.get("command"):
            print(f"check     exit {last.get('exit_code')}  {last.get('command')}")
        if snap.get("state") == "error" and snap.get("error"):
            print(f"error     {snap.get('error')}")
    if want_bag:
        recover_stale_bag(settings.state_dir())
        bag = load_bag(settings.state_dir())
        print(f"bag       {bag.get('state') or 'none'}")
        print(f"bag_id    {bag.get('bag_id') or ''}")
        print(f"halt_bag  {str(bool(bag.get('halt_bag'))).lower()}")
        for t in bag.get("targets") or []:
            if not isinstance(t, dict):
                continue
            print(
                f"target    {t.get('role') or ''}  {t.get('state') or ''}  "
                f"{t.get('name') or ''}  {t.get('halt_reason') or t.get('error') or ''}"
            )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    httpd = serve_deck(settings, demo=bool(args.demo))
    host, port = httpd.server_address[:2]
    print(f"command deck  http://{host}:{port}")
    print(f"loopscope     http://127.0.0.1:{settings.loopscope_port}")
    print(f"roots         {', '.join(str(p) for p in settings.roots)}")
    print(f"mock          {settings.mock}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nhalt")
    finally:
        httpd.server_close()
    return 0


def _cmm_snapshot(settings: Settings, forum: dict) -> dict:
    """Histogram over the population, written to home/cmm.json + cmm.html on every compute."""
    home = settings.state_dir()
    snap = histogram(population(settings), forum, home=home, roots=settings.roots)
    write_cmm(home, snap)
    return snap


def cmd_morning_portfolio(args: argparse.Namespace) -> int:
    """`morning --portfolio`: forum.md (regenerated), the CMM histogram, then the land lines.

    `--diff` / `--branch` / `--json` are per-repo flags and are ignored here.
    """
    settings = _settings_from(args)
    home = settings.home
    if not (home / FORUM_REL).is_file():
        print("no forum yet", file=sys.stderr)
        return 1
    forum = load_forum(home)
    print(render_forum_md(forum, home=home), end="")
    print()
    print(render_cmm_md(_cmm_snapshot(settings, forum)), end="")
    print()
    print("## Land")
    for line in land_lines(forum) or ["- (none)"]:
        print(line)
    return 0


def cmd_morning(args: argparse.Namespace) -> int:
    if getattr(args, "portfolio", False):
        return cmd_morning_portfolio(args)
    if not getattr(args, "repo", None):
        print("repo required", file=sys.stderr)
        return 1
    repo = Path(args.repo)
    branch = getattr(args, "branch", None)
    try:
        view = night_view(repo, branch=branch)
    except FileNotFoundError:
        print("no night", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(view, indent=2, default=str))
    else:
        print(render_terminal(view), end="")
    if getattr(args, "diff", False):
        base = (view.get("base") or {}).get("sha") or (view.get("base") or {}).get("ref")
        br = view.get("branch") or "HEAD"
        proc = git(
            repo,
            "diff",
            f"{base}...{br}",
            "--",
            ".",
            ":!.nightshift",
            check=False,
        )
        sys.stdout.write(proc.stdout)
    remaining = int(view.get("remaining") or 0)
    voided = int(view.get("voided") or 0)
    if remaining or voided:
        return 2
    return 0


def cmd_turns(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    branch = getattr(args, "branch", None) or ""
    text = ""
    if branch:
        if not _NIGHT_BRANCH.match(branch):
            print("no tape", file=sys.stderr)
            return 1
        proc = git(repo, "show", f"{branch}:.nightshift/turns.jsonl", check=False)
        if proc.returncode != 0:
            print("no tape", file=sys.stderr)
            return 1
        text = proc.stdout
        rows = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    else:
        rows = load_turns(repo)
    if not rows:
        print("no tape", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2))
        return 0
    stalls: dict[int, list[str]] = {}
    for row in rows:
        if row.get("kind") == "freeze":
            print(f"T0 freeze void={row.get('void_ids')}")
            continue
        check = row.get("check") or {}
        void = row.get("void") or {}
        mark = "VOID" if void else ("DONE" if row.get("upgrade_id") in (row.get("done_ids_after") or []) else "")
        print(
            f"T{row.get('turn')} | #{row.get('upgrade_id')} | "
            f"wrote {len(row.get('written') or [])} | "
            f"exit {check.get('exit_code')} | "
            f"reverted {len(row.get('reverted') or [])} | "
            f"{mark} | {row.get('commit') or ''} | "
            f"{sum((row.get('secs') or {}).values()) if isinstance(row.get('secs'), dict) else ''}"
        )
        uid = int(row.get("upgrade_id") or 0)
        fp = str((check or {}).get("fingerprint") or "")
        stalls.setdefault(uid, []).append(fp)
    print("stalls:")
    for uid, fps in stalls.items():
        run = 1
        last = ""
        best = 1
        best_fp = ""
        for fp in fps:
            if fp and fp == last:
                run += 1
            else:
                run = 1
            if run > best:
                best = run
                best_fp = fp
            last = fp
        if best >= 2 and best_fp:
            print(f"  job {uid}: same failure x{best} — {best_fp}")
    return 0


def cmd_halt(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    home = settings.state_dir()
    recover_stale_bag(home)
    bag = load_bag(home)
    bag_live = bag.get("state") == "running" and pid_alive(bag.get("runner_pid"))
    board = StatusBoard(home)
    snap = board.snapshot()
    night_live = snap.get("state") == "running" and snap.get("runner_pid")
    if not bag_live and not night_live:
        print("no shift running", file=sys.stderr)
        return 1
    if bag_live:
        mutate_bag(home, lambda current: current.update(halt_bag=True))
    if night_live:
        request_halt(home, int(snap["runner_pid"]))
        print(f"halt requested after turn {snap.get('turn') or 0}")
    else:
        print("halt requested for bag")
    return 0


def cmd_bag(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    plan = select_bag(
        settings,
        size=getattr(args, "size", None),
        skip_meta=bool(getattr(args, "skip_meta", False)) or None,
        meta_last=bool(getattr(args, "meta_last", False)),
    )
    print(render_bag_table(plan))
    if not plan.targets:
        return 1
    if not bool(getattr(args, "run", False)):
        return 0
    result = run_bag(plan, settings)
    print(f"bag_state\t{result.get('state')}")
    return bag_exit_code(result)


def cmd_forum(args: argparse.Namespace) -> int:
    """`nightshift forum` prints forum.md regenerated from forum.json; `--json` the file.

    `ingest` projects clone + home ledgers of the population (roots plus the
    meta checkout); `mark-merged REPO [NIGHT]` stamps one night (NIGHT
    omitted: the most recent done + unmerged night only) and prints it.
    """
    settings = _settings_from(args)
    sub = getattr(args, "forum_cmd", None)
    if sub == "ingest":
        stats: dict[str, int] = {}
        ingest_forum(settings.state_dir(), population(settings), stats=stats)
        print(
            f"repos {stats.get('repos', 0)}  nights {stats.get('nights', 0)}  "
            f"items {stats.get('items', 0)}  orphans {stats.get('orphans', 0)}"
        )
        return 0
    if sub == "mark-merged":
        home = settings.state_dir()
        repo = Path(args.repo)
        wanted = getattr(args, "night", None) or None
        # Pick under no lock, stamp by explicit night under forum.lock: the
        # SafetyError for a typo or an all-merged history surfaces before any write.
        night = str(select_mark_merged_night(load_forum(home), repo, wanted).get("night") or "")
        mark_merged(home, repo, night=night)
        print(f"merged\t{repo.expanduser().resolve().name}\t{night}")
        return 0
    home = settings.home
    if not (home / FORUM_REL).is_file():
        print("no forum yet", file=sys.stderr)
        return 1
    forum = load_forum(home)
    if getattr(args, "json", False):
        print(json.dumps(forum, indent=2))
    else:
        print(render_forum_md(forum, home=home), end="")
    return 0


def cmd_cmm(args: argparse.Namespace) -> int:
    """`nightshift cmm`: histogram + per-repo rows from the forum; `--json` the cmm.json shape."""
    settings = _settings_from(args)
    snap = _cmm_snapshot(settings, load_forum(settings.state_dir()))
    if getattr(args, "json", False):
        print(json.dumps(snap, indent=2))
    else:
        print(render_cmm_md(snap), end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nightshift",
        description="Local overnight coding agent. Pick a repo, sleep, wake up to a branch.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list git repos under configured roots")
    p_list.add_argument("--roots", nargs="*", help="override NIGHTSHIFT_ROOTS / ~/REPOS")
    p_list.add_argument("--include-deprecated", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_run = sub.add_parser("run", help="run an overnight shift against a git work tree")
    p_run.add_argument("repo", help="path to an existing git project")
    p_run.add_argument("--mock", action="store_true", help="offline mock writer/critic")
    p_run.add_argument("--push", action="store_true", help="git push the night branch (off by default)")
    p_run.add_argument("--halt-at", help="local clock halt HH:MM (default 06:00)")
    p_run.add_argument("--max-turns", type=int, help="Ralph turn cap (default 20)")
    p_run.add_argument("--no-observe", action="store_true", help="skip LoopScope bind")
    p_run.add_argument("--roots", nargs="*")
    p_run.add_argument("--brief-size", type=int, help="frozen brief length 2-5 (default 2)")
    p_run.add_argument("--allow-dirty", action="store_true", help="keep pre-existing dirt out of the night")
    p_run.add_argument("--dry-run", action="store_true", help="freeze a brief, write nothing")
    p_run.add_argument("--json", action="store_true", help="with --dry-run, print the brief as JSON")
    p_run.add_argument("--job-turns", type=int, help="per-job turn budget before rotation (default 4)")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="print the last/current shift")
    p_status.add_argument("--json", action="store_true", help="full status.json")
    p_status.add_argument("--bag", action="store_true", help="also print bag.json (merged with --json)")
    p_status.set_defaults(func=cmd_status)

    p_bag = sub.add_parser("bag", help="select tonight's targets, or --run them sequentially")
    p_bag.add_argument("--run", action="store_true", help="take the bag lock and run nights in order")
    p_bag.add_argument("--skip-meta", action="store_true", help="drop Nightshift from the candidate set")
    p_bag.add_argument("--meta-last", action="store_true", help="put meta Nightshift at the end of the bag")
    p_bag.add_argument("--size", type=int, help="bag size 1-3 (default 2)")
    p_bag.add_argument("--allow-dirty", action="store_true")
    p_bag.add_argument("--mock", action="store_true")
    p_bag.add_argument("--roots", nargs="*")
    p_bag.add_argument("--include-deprecated", action="store_true")
    p_bag.add_argument("--halt-at", help="shared clock halt HH:MM (default 06:00)")
    p_bag.add_argument("--max-turns", type=int)
    p_bag.add_argument("--brief-size", type=int)
    p_bag.add_argument("--job-turns", type=int)
    p_bag.add_argument("--no-observe", action="store_true")
    p_bag.add_argument("--push", action="store_true")
    p_bag.set_defaults(func=cmd_bag)

    p_serve = sub.add_parser("serve", help="stdlib HTTP command deck")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--roots", nargs="*")
    p_serve.add_argument("--mock", action="store_true")
    p_serve.add_argument("--demo", action="store_true", help="seed a failing widget repo")
    p_serve.add_argument("--include-deprecated", action="store_true")
    p_serve.add_argument("--no-observe", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_morning = sub.add_parser("morning", help="7am read of the last night on a repo, or --portfolio")
    p_morning.add_argument("repo", nargs="?", help="path to the repo (omit with --portfolio)")
    p_morning.add_argument("--branch", help="night/… branch to read")
    p_morning.add_argument("--json", action="store_true")
    p_morning.add_argument("--diff", action="store_true", help="stream git diff base...branch")
    p_morning.add_argument(
        "--portfolio",
        action="store_true",
        help="forum.md + CMM histogram + land lines for every repo (ignores --diff)",
    )
    p_morning.add_argument("--roots", nargs="*", help="with --portfolio: override NIGHTSHIFT_ROOTS")
    p_morning.add_argument("--include-deprecated", action="store_true")
    p_morning.set_defaults(func=cmd_morning)

    p_turns = sub.add_parser("turns", help="print .nightshift/turns.jsonl")
    p_turns.add_argument("repo")
    p_turns.add_argument("--branch", help="read the tape from a night/* branch")
    p_turns.add_argument("--json", action="store_true")
    p_turns.set_defaults(func=cmd_turns)

    p_halt = sub.add_parser("halt", help="stop the running shift after the current turn")
    p_halt.set_defaults(func=cmd_halt)

    p_forum = sub.add_parser("forum", help="portfolio forum: print forum.md, --json, or ingest")
    p_forum.add_argument("--json", action="store_true", help="print forum.json")
    p_forum.set_defaults(func=cmd_forum, forum_cmd=None)
    forum_sub = p_forum.add_subparsers(dest="forum_cmd")
    p_forum_ingest = forum_sub.add_parser(
        "ingest", help="latest-entry projection of clone + home ledgers under roots"
    )
    p_forum_ingest.add_argument("--roots", nargs="*", help="override NIGHTSHIFT_ROOTS / ~/REPOS")
    p_forum_ingest.add_argument("--include-deprecated", action="store_true")
    p_forum_ingest.set_defaults(func=cmd_forum)
    p_forum_mark = forum_sub.add_parser(
        "mark-merged", help="operator evidence: stamp one forum night merged (cherry-picked keepers)"
    )
    p_forum_mark.add_argument("repo", help="path to the repo")
    p_forum_mark.add_argument(
        "night", nargs="?", help="night/… (default: the most recent done + unmerged night only)"
    )
    p_forum_mark.set_defaults(func=cmd_forum)

    p_cmm = sub.add_parser("cmm", help="evidence histogram from the forum; writes cmm.json + cmm.html")
    p_cmm.add_argument("--json", action="store_true", help="print the cmm.json snapshot")
    p_cmm.add_argument("--roots", nargs="*", help="override NIGHTSHIFT_ROOTS / ~/REPOS")
    p_cmm.add_argument("--include-deprecated", action="store_true")
    p_cmm.set_defaults(func=cmd_cmm)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except SafetyError as exc:
        print(f"error\t{exc}", file=sys.stderr)
        print("branch\t", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"nightshift: {exc}", file=sys.stderr)
        return 1
