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

from .config import Settings
from .models import BRIEF_SIZE_MAX, BRIEF_SIZE_MIN, FrozenBriefError
from .demo import seed_widget
from .repos import find_repos
from .runner import run_night
from .status import StatusBoard


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
        return status

    def snapshot(self) -> dict[str, Any]:
        with self._run_lock:
            return self._reconcile_status_locked().to_dict()

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

    def start_run(self, path: str, mock: bool | None, brief_size: int | None = None) -> dict[str, Any]:
        from .models import clamp_brief_size

        size = clamp_brief_size(
            brief_size if brief_size is not None else self.settings.brief_size
        )
        settings = Settings(
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
            brief_size=size,
        )

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

        with self._run_lock:
            if self._reconcile_status_locked().state == "running":
                return {"ok": False, "error": "a shift is already running"}
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
                self._json(200, {"repos": deck.repos(include)})
                return
            if parsed.path == "/api/status":
                self._json(200, deck.snapshot())
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
                    )
                except FrozenBriefError as exc:
                    self._json(400, {"error": str(exc)})
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
