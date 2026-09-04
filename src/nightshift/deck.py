"""Stdlib HTTP command deck. No React, no Tailwind-as-a-framework."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .bag import (
    acquire_bag,
    assert_shift_idle,
    load_bag,
    load_merged_status,
    pid_alive,
    plan_to_dict,
    recover_stale_bag,
    run_bag,
    save_bag,
    select_bag,
)
from .cmm import histogram, population, render_cmm_html, write_cmm
from .config import Settings
from .forum import load_forum
from .models import BRIEF_SIZE_MAX, BRIEF_SIZE_MIN, FrozenBriefError, SafetyError
from .demo import seed_widget
from .repos import find_repos
from .runner import dry_run_brief, run_night
from .safety import assert_clean_tree, assert_safe_target
from .status import StatusBoard, request_halt


def _html() -> bytes:
    return resources.files("nightshift").joinpath("deck.html").read_bytes()


class DeckState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.board = StatusBoard(settings.state_dir())
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.snapshot()

    def _live_owner(self, runner_pid: int | None) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        if runner_pid is None or runner_pid == os.getpid():
            return False
        try:
            os.kill(runner_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _reconcile_status_locked(self):
        status = self.board.read()
        if status.state == "running" and not self._live_owner(status.runner_pid):
            status = self.board.update(
                state="halted",
                runner_pid=None,
                brain="",
                halt_reason="interrupted",
                error=(
                    "The previous shift was interrupted. "
                    "Select a repository and press Run to start again."
                ),
            )
        requested = False
        if status.state == "running" and status.runner_pid:
            path = self.settings.state_dir() / "halt.request"
            requested = path.is_file()
        if status.halt_requested != requested:
            status = self.board.update(halt_requested=requested)
        return status

    def snapshot(self) -> dict[str, Any]:
        with self._run_lock:
            self._reconcile_status_locked()
            return load_merged_status(self.settings.state_dir())

    def repos(self, include_deprecated: bool) -> list[dict[str, Any]]:
        return [
            {
                "path": r.path,
                "name": r.name,
                "branch": r.branch,
                "dirty": r.dirty,
                "deprecated": r.deprecated,
            }
            for r in find_repos(
                self.settings.roots, include_deprecated=include_deprecated
            )
        ]

    def _copy_settings(
        self,
        *,
        mock: bool | None,
        brief_size: int,
        allow_dirty: bool,
        dry_run: bool,
    ) -> Settings:
        return Settings(
            writer_base_url=self.settings.writer_base_url,
            writer_model=self.settings.writer_model,
            critic_base_url=self.settings.critic_base_url,
            critic_model=self.settings.critic_model,
            api_key=self.settings.api_key,
            roots=self.settings.roots,
            halt_at=self.settings.halt_at,
            max_turns=self.settings.max_turns,
            check_timeout=self.settings.check_timeout,
            mock=self.settings.mock if mock is None else mock,
            push=False,
            include_deprecated=self.settings.include_deprecated,
            observe=self.settings.observe,
            home=self.settings.home,
            loopscope_port=self.settings.loopscope_port,
            writer_timeout=self.settings.writer_timeout,
            critic_timeout=self.settings.critic_timeout,
            stall_after=self.settings.stall_after,
            brief_size=brief_size,
            job_turns=self.settings.job_turns,
            allow_dirty=allow_dirty,
            dry_run=dry_run,
            bag_size=self.settings.bag_size,
            skip_meta=self.settings.skip_meta,
            meta_last=self.settings.meta_last,
            bag_min_minutes=self.settings.bag_min_minutes,
            forum_enabled=self.settings.forum_enabled,
        )

    def start_run(
        self,
        path: str,
        mock: bool | None,
        brief_size: int | None = None,
        *,
        allow_dirty: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        from .models import clamp_brief_size

        size = clamp_brief_size(
            brief_size if brief_size is not None else self.settings.brief_size
        )
        settings = self._copy_settings(
            mock=mock, brief_size=size, allow_dirty=allow_dirty, dry_run=dry_run
        )

        with self._run_lock:
            if self._reconcile_status_locked().state == "running":
                return {"ok": False, "error": "a shift is already running"}
            if not dry_run:
                home = self.settings.state_dir()
                recover_stale_bag(home)
                try:
                    assert_shift_idle(home, allow_self=False)
                except SafetyError as exc:
                    return {"ok": False, "error": str(exc)}
                if self._thread is not None and self._thread.is_alive():
                    return {"ok": False, "error": "a shift is already running"}
            target = assert_safe_target(Path(path), explicit=True)
            if dry_run:
                brief = dry_run_brief(target, settings, explicit=True)
                return {"ok": True, "dry_run": True, "brief": brief.to_dict()}
            assert_clean_tree(target, allow_dirty=allow_dirty)

            def _go() -> None:
                try:
                    run_night(Path(path), settings, explicit=True)
                except Exception as exc:
                    self.board.update(
                        state="error",
                        runner_pid=None,
                        error=str(exc),
                        brain="",
                    )

            self.board.update(
                state="running",
                runner_pid=os.getpid(),
                repo=path,
                error="",
                summary="",
            )
            self._thread = threading.Thread(target=_go, daemon=True)
            self._thread.start()
        return {"ok": True, "repo": path, "mock": settings.mock, "brief_size": size}

    def start_bag(
        self,
        *,
        dry: bool = True,
        size: int | None = None,
        skip_meta: bool = False,
        meta_last: bool = False,
        mock: bool | None = None,
        allow_dirty: bool = False,
        brief_size: int | None = None,
    ) -> dict[str, Any]:
        from .models import clamp_brief_size

        jobs = clamp_brief_size(
            brief_size if brief_size is not None else self.settings.brief_size
        )
        settings = self._copy_settings(
            mock=mock, brief_size=jobs, allow_dirty=allow_dirty, dry_run=dry
        )
        if skip_meta:
            settings.skip_meta = True
        if meta_last:
            settings.meta_last = True
        with self._run_lock:
            if self._reconcile_status_locked().state == "running":
                return {"ok": False, "error": "a shift is already running"}
            home = self.settings.state_dir()
            recover_stale_bag(home)
            try:
                assert_shift_idle(home, allow_self=False)
            except SafetyError as exc:
                return {"ok": False, "error": str(exc)}
            if self._thread is not None and self._thread.is_alive():
                return {"ok": False, "error": "a shift is already running"}
            try:
                plan = select_bag(
                    settings, size=size, skip_meta=skip_meta or None, meta_last=meta_last
                )
            except SafetyError as exc:
                return {"ok": False, "error": str(exc)}
            if dry:
                return {"ok": True, "dry": True, "bag": plan_to_dict(plan)}
            if not plan.targets:
                return {"ok": False, "error": "no targets"}
            acquire_bag(
                home,
                {
                    **plan_to_dict(plan),
                    "state": "running",
                    "runner_pid": os.getpid(),
                    "halt_bag": False,
                    "mock": settings.mock,
                    "brief_size": jobs,
                },
            )

            def _go() -> None:
                try:
                    run_bag(plan, settings)
                except Exception as exc:
                    self.board.update(
                        state="error",
                        runner_pid=None,
                        error=str(exc),
                        brain="",
                    )

            self._thread = threading.Thread(target=_go, daemon=True)
            self._thread.start()
        return {"ok": True, "dry": False, "bag_id": plan.bag_id, "mock": settings.mock, "brief_size": jobs}

    def cmm_snapshot(self) -> dict[str, Any]:
        home = self.settings.state_dir()
        snap = histogram(
            population(self.settings),
            load_forum(home),
            home=home,
            roots=self.settings.roots,
        )
        write_cmm(home, snap)
        return snap

    def request_halt(self) -> dict[str, Any]:
        with self._run_lock:
            home = self.settings.state_dir()
            recover_stale_bag(home)
            bag = load_bag(home)
            bag_live = bag.get("state") == "running" and pid_alive(bag.get("runner_pid"))
            status = self._reconcile_status_locked()
            night_live = status.state == "running" and status.runner_pid
            if not bag_live and not night_live:
                return {"ok": False, "error": "no shift running"}
            if bag_live:
                bag["halt_bag"] = True
                save_bag(home, bag)
            if night_live:
                request_halt(home, int(status.runner_pid))
                self.board.update(halt_requested=True)
            return {"ok": True, "after_turn": status.turn}


def make_handler(deck: DeckState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return None

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            self._send(
                code,
                json.dumps(payload).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self._send(200, _html(), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/repos":
                qs = parse_qs(parsed.query)
                include = (qs.get("include_deprecated") or ["0"])[0] in {"1", "true", "yes"}
                include = include or deck.settings.include_deprecated
                self._json(200, {"repos": deck.repos(include)})
                return
            if parsed.path == "/api/status":
                self._json(200, deck.snapshot())
                return
            if parsed.path == "/api/bag":
                bag = load_bag(deck.settings.state_dir())
                self._json(200, bag if bag else {"targets": []})
                return
            if parsed.path == "/api/forum":
                self._json(200, load_forum(deck.settings.state_dir()))
                return
            if parsed.path == "/api/cmm":
                self._json(200, deck.cmm_snapshot())
                return
            if parsed.path == "/cmm":
                html = render_cmm_html(deck.cmm_snapshot()).encode("utf-8")
                self._send(200, html, "text/html; charset=utf-8")
                return
            if parsed.path == "/api/config":
                self._json(
                    200,
                    {
                        "mock": deck.settings.mock,
                        "roots": [str(p) for p in deck.settings.roots],
                        "loopscope_url": f"http://127.0.0.1:{deck.settings.loopscope_port}",
                        "halt_at": deck.settings.halt_at,
                        "brief_size": deck.settings.brief_size,
                        "brief_size_min": BRIEF_SIZE_MIN,
                        "brief_size_max": BRIEF_SIZE_MAX,
                        "max_turns": deck.settings.max_turns,
                        "writer_timeout": deck.settings.writer_timeout,
                        "critic_timeout": deck.settings.critic_timeout,
                        "check_timeout": deck.settings.check_timeout,
                        "include_deprecated": deck.settings.include_deprecated,
                    },
                )
                return
            if parsed.path == "/api/summary":
                snap = deck.snapshot()
                self._json(200, {"summary": snap.get("summary") or ""})
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid json"})
                return
            if parsed.path == "/api/halt":
                result = deck.request_halt()
                self._json(200 if result.get("ok") else 409, result)
                return
            if parsed.path == "/api/bag":
                mock = payload.get("mock")
                brief_size = payload.get("brief_size", None)
                if brief_size is not None:
                    try:
                        brief_size = int(brief_size)
                    except (TypeError, ValueError) as exc:
                        self._json(400, {"error": str(exc)})
                        return
                size = payload.get("size", None)
                if size is not None:
                    try:
                        size = int(size)
                    except (TypeError, ValueError) as exc:
                        self._json(400, {"error": str(exc)})
                        return
                try:
                    result = deck.start_bag(
                        dry=bool(payload.get("dry", True)),
                        size=size,
                        skip_meta=bool(payload.get("skip_meta")),
                        meta_last=bool(payload.get("meta_last")),
                        mock=mock if isinstance(mock, bool) else None,
                        allow_dirty=bool(payload.get("allow_dirty")),
                        brief_size=brief_size,
                    )
                except FrozenBriefError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                except SafetyError as exc:
                    self._json(409, {"ok": False, "error": str(exc)})
                    return
                self._json(200 if result.get("ok") else 409, result)
                return
            if parsed.path == "/api/run":
                path = str(payload.get("path") or "").strip()
                if not path:
                    self._json(400, {"error": "path required"})
                    return
                mock = payload.get("mock")
                brief_size = payload.get("brief_size", None)
                if brief_size is not None:
                    try:
                        brief_size = int(brief_size)
                    except (TypeError, ValueError) as exc:
                        self._json(400, {"error": str(exc)})
                        return
                try:
                    result = deck.start_run(
                        path,
                        mock if isinstance(mock, bool) else None,
                        brief_size=brief_size,
                        allow_dirty=bool(payload.get("allow_dirty")),
                        dry_run=bool(payload.get("dry_run")),
                    )
                except FrozenBriefError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                except SafetyError as exc:
                    self._json(409, {"ok": False, "error": str(exc)})
                    return
                self._json(200 if result.get("ok") else 409, result)
                return
            self._json(404, {"error": "not found"})

    return Handler


def serve_deck(settings: Settings, *, demo: bool = False) -> ThreadingHTTPServer:
    if demo:
        root = settings.state_dir() / "demo-roots"
        widget = root / "widget"
        if not (widget / ".git").exists():
            seed_widget(widget)
        settings.roots = [root]
    deck = DeckState(settings)
    handler = make_handler(deck)
    httpd = ThreadingHTTPServer((settings.deck_host, settings.deck_port), handler)
    return httpd
