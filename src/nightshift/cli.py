"""CLI: list, run, status, serve. Equally first-class with the command deck."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .deck import serve_deck
from .gitops import rev_parse
from .repos import find_repos
from .runner import run_night
from .status import StatusBoard


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
    )


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
    report = run_night(Path(args.repo), settings, explicit=True)
    print(f"branch\t{report.branch}")
    print(f"halt\t{report.halt_reason}")
    print(f"remaining\t{report.remaining_count}")
    print(f"summary\t{report.summary_path}")
    print(f"main\t{report.main_ref} {report.main_sha}")
    try:
        still = rev_parse(report.repo, report.main_ref)
        print(f"main_now\t{still}")
    except Exception:
        pass
    return 0 if report.halt_reason == "remaining_zero" else 2


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    board = StatusBoard(settings.state_dir())
    snap = board.snapshot()
    if snap.get("state") == "idle" and not snap.get("repo"):
        print("no overnight run recorded")
        return 1
    print(json.dumps(snap, indent=2))
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
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="print the last/current shift")
    p_status.set_defaults(func=cmd_status)

    p_serve = sub.add_parser("serve", help="stdlib HTTP command deck")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=43171)
    p_serve.add_argument("--roots", nargs="*")
    p_serve.add_argument("--mock", action="store_true")
    p_serve.add_argument("--demo", action="store_true", help="seed a failing widget repo")
    p_serve.add_argument("--include-deprecated", action="store_true")
    p_serve.add_argument("--no-observe", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"nightshift: {exc}", file=sys.stderr)
        return 1
