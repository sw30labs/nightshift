from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

from nightshift.config import Settings
from nightshift.deck import DeckState, serve_deck
from nightshift.gitops import current_branch, rev_parse
from nightshift.ledger import merge_night_into_ledger
from nightshift.models import Brief, Upgrade
from nightshift.runner import run_night
from nightshift.status import StatusBoard, request_halt


def test_halt_before_run_stops_at_requested(fixture_repo, mock_settings, ns_home):
    request_halt(ns_home, os.getpid())
    main_sha = rev_parse(fixture_repo, "main")
    report = run_night(fixture_repo, mock_settings, explicit=True)
    assert report.halt_reason == "requested"
    assert report.remaining_count == 2
    assert report.summary_path.is_file()
    assert "## Remaining" in report.summary_path.read_text()
    assert current_branch(fixture_repo).startswith("night/")
    assert rev_parse(fixture_repo, "main") == main_sha
    ledger = json.loads((fixture_repo / ".nightshift" / "ledger.json").read_text())
    assert all(e.get("attempted") is False for e in ledger["entries"])


def test_foreign_pid_ignored(fixture_repo, mock_settings, ns_home):
    request_halt(ns_home, os.getpid() + 99999)
    report = run_night(fixture_repo, mock_settings, explicit=True)
    assert report.halt_reason == "remaining_zero"
    assert report.remaining_count == 0


def test_deck_halt_idle_and_running(tmp_path, ns_home, monkeypatch):
    settings = Settings(mock=True, observe=False, home=ns_home, deck_port=0)
    httpd = serve_deck(settings, demo=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        base = f"http://{host}:{port}"

        def post(path, payload):
            req = urllib.request.Request(
                base + path,
                data=json.dumps(payload).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode())

        code, body = post("/api/halt", {})
        assert code == 409

        gate = threading.Event()

        def blocked(*a, **k):
            gate.wait(timeout=10)
            return None

        monkeypatch.setattr("nightshift.deck.run_night", blocked)
        listed = json.loads(urllib.request.urlopen(base + "/api/repos", timeout=5).read())
        widget = listed["repos"][0]["path"]
        code, started = post("/api/run", {"path": widget, "mock": True})
        assert code == 200, started
        time.sleep(0.2)
        code, halted = post("/api/halt", {})
        assert code == 200, halted
        snap = json.loads(urllib.request.urlopen(base + "/api/status", timeout=5).read())
        assert snap.get("halt_requested") is True
        gate.set()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_merge_attempted_ids():
    brief = Brief.freeze(
        [
            Upgrade(1, "a", "true 1", ["a.py"]),
            Upgrade(2, "b", "true 2", ["b.py"]),
        ]
    )
    ledger = merge_night_into_ledger(
        {"entries": []}, brief, "night/x", attempted_ids={2}
    )
    by = {e["title"]: e for e in ledger["entries"]}
    assert by["a"]["attempted"] is False
    assert by["b"]["attempted"] is True
    default = merge_night_into_ledger({"entries": []}, brief, "night/y")
    by2 = {e["title"]: e for e in default["entries"]}
    assert by2["a"]["attempted"] is True
    _ = DeckState
    _ = StatusBoard
