"""LoopScope hook. Prefer the real package; degrade to JSONL + a tiny stdlib page."""

from __future__ import annotations

import json
import shutil
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class _NullScope:
    def hold(self) -> None:
        return None

    def stop(self) -> None:
        return None


class FallbackScope:
    def __init__(self, server: ThreadingHTTPServer | None, jsonl: Path | None) -> None:
        self._server = server
        self.jsonl = jsonl
        self._thread: threading.Thread | None = None

    def hold(self) -> None:
        if self._thread:
            self._thread.join()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()


def _try_loopscope():
    try:
        import loopscope  # type: ignore

        return loopscope
    except ImportError:
        return None


_fallback_events: list[dict[str, Any]] = []
_fallback_lock = threading.Lock()
_jsonl_path: Path | None = None


def log(text: str, level: str = "info", **payload: Any) -> None:
    ls = _try_loopscope()
    if ls is not None:
        ls.log(text, level=level, **payload)
        return
    event = {"kind": "log", "level": level, "text": text, **payload}
    _record(event)


def metric(name: str, value: float, **payload: Any) -> None:
    ls = _try_loopscope()
    if ls is not None:
        ls.metric(name, value, **payload)
        return
    _record({"kind": "metric", "name": name, "value": value, **payload})


def _record(event: dict[str, Any]) -> None:
    with _fallback_lock:
        _fallback_events.append(event)
        if _jsonl_path is not None:
            _jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with _jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")


def rotate_events_jsonl(jsonl_path: Path) -> Path | None:
    """Copy events.jsonl (and brief/summary if present) into history/{UTC stamp}/ before wipe."""
    path = Path(jsonl_path)
    if not path.is_file():
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= 0:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    hist_dir = path.parent / "history" / stamp
    hist_dir.mkdir(parents=True, exist_ok=True)
    dest = hist_dir / "events.jsonl"
    shutil.copy2(path, dest)
    for name in ("brief.json", "summary.md"):
        src = path.parent / name
        if src.is_file():
            shutil.copy2(src, hist_dir / name)
    return dest


def start(
    *,
    open_browser: bool = False,
    jsonl: str | Path | None = None,
    port: int = 7788,
    host: str = "127.0.0.1",
    serve: bool = True,
):
    """One hook. Same shape as loopscope.start()."""
    global _jsonl_path
    if jsonl:
        _jsonl_path = Path(jsonl)
        _jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        rotate_events_jsonl(_jsonl_path)
        _jsonl_path.write_text("", encoding="utf-8")
    ls = _try_loopscope()
    if ls is not None:
        try:
            return ls.start(
                host=host,
                port=port,
                open_browser=open_browser,
                jsonl=str(_jsonl_path) if _jsonl_path else None,
            )
        except Exception as exc:
            # Busy port, jsonl already attached, uvicorn glitch — the night still runs.
            _record(
                {
                    "kind": "log",
                    "level": "warn",
                    "text": f"{exc}; continuing without a new dashboard",
                }
            )
            return _NullScope()
    if not serve:
        return FallbackScope(None, _jsonl_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/events"):
                with _fallback_lock:
                    body = json.dumps(_fallback_events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            html = FALLBACK_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError:
        return FallbackScope(None, _jsonl_path)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    scope = FallbackScope(httpd, _jsonl_path)
    scope._thread = thread
    if open_browser:
        webbrowser.open(f"http://{host}:{port}")
    return scope


def attach(app, **kwargs):
    ls = _try_loopscope()
    if ls is not None:
        return ls.attach(app, **kwargs)
    return {"callbacks": []}


def finish(config, **payload):
    ls = _try_loopscope()
    if ls is not None:
        return ls.finish(config, **payload)
    _record({"kind": "finish", **payload})
    return None


class FallbackRalphLoop:
    def __init__(
        self,
        objective: str,
        *,
        phases=None,
        max_iters: int = 25,
        stall_after: int = 8,
        roles=None,
        **_kwargs,
    ) -> None:
        self.objective = objective
        self.phases = list(phases or ["job", "writer", "check", "score"])
        self.max_iters = max_iters
        self.stall_after = stall_after
        self.roles = roles or {}
        self._done = False

    def attach_graph(self, app, config=None):
        merged = dict(config or {})
        merged.setdefault("callbacks", [])
        return merged

    def __iter__(self):
        for i in range(self.max_iters):
            if self._done:
                break
            yield FallbackIteration(self, i)


class FallbackIteration:
    def __init__(self, loop: FallbackRalphLoop, index: int) -> None:
        self.loop = loop
        self.index = index
        self._done_reason = None

    def signal(self, value: float, name: str = "signal") -> None:
        metric(name, float(value))

    def done(self, reason: str = "converged") -> None:
        self._done_reason = reason
        self.loop._done = True
        log(reason, level="info")

    def note(self, text: str) -> None:
        log(text)

    def phase(self, name: str):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            log(f"phase {name}")
            yield self

        return _cm()


def ralph_loop(objective: str, **kwargs):
    ls = _try_loopscope()
    if ls is not None:
        return ls.RalphLoop(objective, **kwargs)
    return FallbackRalphLoop(objective, **kwargs)


FALLBACK_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/>
<title>Nightshift · LoopScope fallback</title>
<style>
  body { background:#161310; color:#e8dfd2; font:14px ui-monospace,monospace; margin:2rem; }
  h1 { color:#c44928; letter-spacing:.2em; font-size:14px; }
  pre { white-space:pre-wrap; }
</style></head>
<body>
<h1>LOOPSCOPE FALLBACK</h1>
<p>loopscope is not installed. Events are JSONL + this page.</p>
<pre id="log">loading…</pre>
<script>
async function tick(){
  const r = await fetch('/events');
  const data = await r.json();
  document.getElementById('log').textContent = JSON.stringify(data, null, 2);
}
tick(); setInterval(tick, 1000);
</script>
</body></html>
"""
