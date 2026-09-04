from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from nightshift import cli
from nightshift.gitops import current_branch, list_local_branches
from nightshift.models import SafetyError
from nightshift.runner import dry_run_brief, run_night
from nightshift.status import StatusBoard


def test_dry_run_cli_writes_nothing(fixture_repo, mock_settings, ns_home, capsys):
    code = cli.main(["run", str(fixture_repo), "--mock", "--dry-run", "--no-observe"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Make test_add pass" in out or "test_add" in out
    assert "Make test_greet pass" in out or "test_greet" in out
    branches = list_local_branches(fixture_repo)
    assert not any(b.startswith("night/") for b in branches)
    assert not (fixture_repo / ".nightshift").exists()
    snap = StatusBoard(ns_home).snapshot()
    assert snap.get("state") in {"idle", ""} or not snap.get("repo") or snap.get("state") != "running"
    # dry-run never publishes: no forum under home
    assert not (ns_home / "forum.json").exists()
    assert not (ns_home / "forum.md").exists()


def test_dry_run_json(fixture_repo, capsys):
    code = cli.main(["run", str(fixture_repo), "--mock", "--dry-run", "--json", "--no-observe"])
    assert code == 0
    # python\t line then JSON
    out = capsys.readouterr().out
    blob = out[out.find("{") :]
    data = json.loads(blob)
    assert len(data["upgrades"]) == 2


def test_freeze_failure_leaves_main(fixture_repo, mock_settings, monkeypatch):
    class Boom:
        mock = False

        def propose_brief(self, *a, **k):
            raise RuntimeError("critic down")

        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr("nightshift.runner.Critic", Boom)
    with pytest.raises(RuntimeError, match="critic down"):
        run_night(fixture_repo, mock_settings, explicit=True)
    assert current_branch(fixture_repo) == "main"
    assert not any(b.startswith("night/") for b in list_local_branches(fixture_repo))
    board = StatusBoard(mock_settings.home)
    assert board.snapshot()["state"] == "error"


def test_persist_failure_returns_to_base(fixture_repo, mock_settings, monkeypatch):
    real_persist = __import__("nightshift.runner", fromlist=["persist_brief"]).persist_brief

    def boom(ctx, brief):
        raise RuntimeError("disk full")

    monkeypatch.setattr("nightshift.runner.persist_brief", boom)
    with pytest.raises(SafetyError, match="git branch -d"):
        run_night(fixture_repo, mock_settings, explicit=True)
    assert current_branch(fixture_repo) == "main"
    nights = [b for b in list_local_branches(fixture_repo) if b.startswith("night/")]
    assert nights  # empty branch left


def test_halt_at_invalid_raises_before_branch(fixture_repo, mock_settings):
    mock_settings.halt_at = "abc"
    with pytest.raises(SafetyError, match="halt_at"):
        run_night(fixture_repo, mock_settings, explicit=True)
    assert current_branch(fixture_repo) == "main"
    assert not any(b.startswith("night/") for b in list_local_branches(fixture_repo))


def test_brain_probe_live_and_unreachable(fixture_repo, mock_settings):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return None

        def do_GET(self):
            body = json.dumps({"data": [{"id": "GLM-5.3-Flash-MLX-8bit"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        host, port = httpd.server_address[:2]
        mock_settings.mock = False
        mock_settings.critic_base_url = f"http://{host}:{port}/v1"
        mock_settings.writer_base_url = f"http://{host}:{port}/v1"
        mock_settings.critic_model = "GLM-5.3-Flash-MLX-8bit"
        mock_settings.writer_model = "GLM-5.3-Flash-MLX-8bit"
        # probe only — then freeze will try to chat and fail. We only check probe via dry_run_brief
        # which calls probe then propose. Chat will fail. So call probe_brains directly.
        from nightshift.runner import probe_brains

        probe_brains(mock_settings)
    finally:
        httpd.shutdown()
        httpd.server_close()

    mock_settings.critic_base_url = "http://127.0.0.1:1/v1"
    mock_settings.writer_base_url = "http://127.0.0.1:1/v1"
    mock_settings.mock = False
    with pytest.raises(SafetyError, match="unreachable"):
        from nightshift.runner import probe_brains

        probe_brains(mock_settings)
    mock_settings.mock = True
    from nightshift.runner import probe_brains

    probe_brains(mock_settings)  # mock skips


def test_deck_dry_run(tmp_path, ns_home):
    from nightshift.config import Settings
    from nightshift.deck import serve_deck
    import threading
    import urllib.request
    import time

    settings = Settings(mock=True, observe=False, home=ns_home, deck_port=0)
    httpd = serve_deck(settings, demo=True)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        host, port = httpd.server_address[:2]
        base = f"http://{host}:{port}"
        listed = json.loads(urllib.request.urlopen(base + "/api/repos", timeout=5).read())
        widget = listed["repos"][0]["path"]
        req = urllib.request.Request(
            base + "/api/run",
            data=json.dumps({"path": widget, "mock": True, "dry_run": True}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
        assert body.get("brief")
        snap = json.loads(urllib.request.urlopen(base + "/api/status", timeout=5).read())
        assert snap.get("state") in {"idle", "halted"} or snap.get("state") != "running"
    finally:
        httpd.shutdown()
        httpd.server_close()
    _ = Path
    _ = time
    _ = dry_run_brief
