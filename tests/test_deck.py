from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from nightshift.config import Settings
from nightshift.deck import serve_deck


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
        return exc.code, body


def test_deck_lists_and_runs_mock(tmp_path, ns_home):
    settings = Settings(
        mock=True,
        observe=False,
        home=ns_home,
        max_turns=12,
        stall_after=12,
        check_timeout=30,
        deck_host="127.0.0.1",
        deck_port=0,
        push=False,
    )
    httpd = serve_deck(settings, demo=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        base = f"http://{host}:{port}"
        with urllib.request.urlopen(base + "/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        assert "NIGHTSHIFT" in html
        assert "RUN" in html
        cfg = _get(base + "/api/config")
        assert cfg["mock"] is True
        listed = _get(base + "/api/repos")
        assert listed["repos"], listed
        widget = listed["repos"][0]["path"]
        assert widget.endswith("widget")
        code, started = _post(base + "/api/run", {"path": widget, "mock": True})
        assert code == 200, started
        deadline = time.time() + 60
        snap = {}
        while time.time() < deadline:
            snap = _get(base + "/api/status")
            if snap.get("state") in {"done", "halted", "error"}:
                break
            time.sleep(0.25)
        assert snap.get("state") == "done", snap
        assert snap.get("remaining_count") == 0
        assert snap.get("halt_reason") == "remaining_zero"
        assert "What changed" in (snap.get("summary") or "")
    finally:
        httpd.shutdown()
        httpd.server_close()
