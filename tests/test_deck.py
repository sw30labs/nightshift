from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

from nightshift.config import Settings
from nightshift.deck import DeckState, serve_deck
from nightshift.status import StatusBoard


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


def test_deck_recovers_interrupted_run_and_accepts_another(
    tmp_path, ns_home, monkeypatch
):
    settings = Settings(mock=True, observe=False, home=ns_home, deck_port=0)
    StatusBoard(settings.state_dir()).update(
        state="running",
        repo="/tmp/interrupted-repo",
        brain="writer",
    )
    monkeypatch.setattr("nightshift.deck.run_night", lambda *args, **kwargs: None)

    httpd = serve_deck(settings, demo=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        base = f"http://{host}:{port}"
        snap = _get(base + "/api/status")
        assert snap["state"] == "halted"
        assert snap["halt_reason"] == "interrupted"
        assert snap["runner_pid"] is None
        assert "previous shift was interrupted" in snap["error"].lower()

        widget = _get(base + "/api/repos")["repos"][0]["path"]
        code, started = _post(base + "/api/run", {"path": widget, "mock": True})
        assert code == 200, started
        assert started["ok"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_deck_preserves_running_status_for_live_external_owner(
    ns_home, monkeypatch
):
    settings = Settings(mock=True, observe=False, home=ns_home, deck_port=0)
    external_pid = os.getpid() + 10_000
    monkeypatch.setattr("nightshift.deck.os.kill", lambda pid, signal: None)
    StatusBoard(settings.state_dir()).update(
        state="running",
        runner_pid=external_pid,
        repo="/tmp/live-repo",
    )

    deck = DeckState(settings)

    assert deck.snapshot()["state"] == "running"
    assert deck.start_run("/tmp/another-repo", True) == {
        "ok": False,
        "error": "a shift is already running",
    }


def test_deck_recovers_running_status_for_dead_external_owner(
    ns_home, monkeypatch
):
    settings = Settings(mock=True, observe=False, home=ns_home, deck_port=0)

    def missing_process(pid, signal):
        raise ProcessLookupError

    monkeypatch.setattr("nightshift.deck.os.kill", missing_process)
    StatusBoard(settings.state_dir()).update(
        state="running",
        runner_pid=os.getpid() + 10_000,
        repo="/tmp/dead-repo",
    )

    snap = DeckState(settings).snapshot()

    assert snap["state"] == "halted"
    assert snap["halt_reason"] == "interrupted"
    assert snap["runner_pid"] is None


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
        assert "brief-size" in html
        assert "JOBS" in html
        assert "id=\"aineko\"" in html
        cfg = _get(base + "/api/config")
        assert cfg["mock"] is True
        assert cfg["brief_size"] == 2
        assert cfg["brief_size_min"] == 2
        assert cfg["brief_size_max"] == 5
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



def test_deck_run_brief_size_2(tmp_path, ns_home):
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
        listed = _get(base + "/api/repos")
        widget = listed["repos"][0]["path"]
        code, started = _post(
            base + "/api/run", {"path": widget, "mock": True, "brief_size": 2}
        )
        assert code == 200, started
        assert started.get("brief_size") == 2
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
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_deck_rejects_brief_size_6(tmp_path, ns_home):
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
        listed = _get(base + "/api/repos")
        widget = listed["repos"][0]["path"]
        code, body = _post(
            base + "/api/run", {"path": widget, "mock": True, "brief_size": 6}
        )
        assert code == 400, body
        assert "error" in body
    finally:
        httpd.shutdown()
        httpd.server_close()
