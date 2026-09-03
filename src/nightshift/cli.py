"""CLI: list, run, status, serve, morning, turns, halt. Equally first-class with the command deck."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .config import Settings
from .deck import serve_deck
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
    report = run_night(Path(args.repo), settings, explicit=True)
    print(f"branch\t{report.branch}")
    print(f"halt\t{report.halt_reason}")
    print(f"remaining\t{report.remaining_count}")
    print(f"summary\t{report.summary_path}")
    print(f"main\t{report.main_ref} {report.main_sha}")
    print(f"base\t{report.base_ref} {report.base_sha}")
    print(f"main_untouched\t{str(report.main_untouched).lower()}")
    landed = sum(1 for u in report.brief.upgrades if u.done)
    print(
        f"verdict\t{landed} of {len(report.brief.upgrades)} landed · "
        f"{report.brief.void_count} void · {report.remaining_count} open"
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
            except PermissionError:
                pass
    if snap.get("state") == "idle" and not snap.get("repo"):
        print("no overnight run recorded")
        return 1
    if getattr(args, "json", False):
        print(json.dumps(snap, indent=2))
        return 0
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


def cmd_morning(args: argparse.Namespace) -> int:
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
    board = StatusBoard(settings.state_dir())
    snap = board.snapshot()
    if snap.get("state") != "running" or not snap.get("runner_pid"):
        print("no shift running", file=sys.stderr)
        return 1
    request_halt(settings.state_dir(), int(snap["runner_pid"]))
    print(f"halt requested after turn {snap.get('turn') or 0}")
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
    p_status.set_defaults(func=cmd_status)

    p_serve = sub.add_parser("serve", help="stdlib HTTP command deck")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--roots", nargs="*")
    p_serve.add_argument("--mock", action="store_true")
    p_serve.add_argument("--demo", action="store_true", help="seed a failing widget repo")
    p_serve.add_argument("--include-deprecated", action="store_true")
    p_serve.add_argument("--no-observe", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_morning = sub.add_parser("morning", help="7am read of the last night on a repo")
    p_morning.add_argument("repo")
    p_morning.add_argument("--branch", help="night/… branch to read")
    p_morning.add_argument("--json", action="store_true")
    p_morning.add_argument("--diff", action="store_true", help="stream git diff base...branch")
    p_morning.set_defaults(func=cmd_morning)

    p_turns = sub.add_parser("turns", help="print .nightshift/turns.jsonl")
    p_turns.add_argument("repo")
    p_turns.add_argument("--branch", help="read the tape from a night/* branch")
    p_turns.add_argument("--json", action="store_true")
    p_turns.set_defaults(func=cmd_turns)

    p_halt = sub.add_parser("halt", help="stop the running shift after the current turn")
    p_halt.set_defaults(func=cmd_halt)

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
