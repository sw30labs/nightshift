from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

from nightshift.bag import save_bag
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


def test_deck_html_has_new_instruments(ns_home):
    settings = Settings(mock=True, observe=False, home=ns_home, deck_port=0)
    httpd = serve_deck(settings, demo=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        base = f"http://{host}:{port}"
        with urllib.request.urlopen(base + "/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        assert 'id="brief"' in html
        assert 'id="bag-select"' in html
        assert 'id="run-bag"' in html
        assert ">BAG<" in html
        assert "RUN BAG" in html
        assert 'href="/cmm"' in html
        assert 'id="halt"' in html
        assert 'id="tohalt"' in html
        assert 'id="turns"' in html
        assert "aineko.resting" in html
        assert "prefers-reduced-motion" in html
        cfg = _get(base + "/api/config")
        assert "max_turns" in cfg
        assert "writer_timeout" in cfg
        assert "critic_timeout" in cfg
        assert "check_timeout" in cfg
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_deck_run_exposes_brief_and_turns(tmp_path, ns_home):
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
        widget = _get(base + "/api/repos")["repos"][0]["path"]
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
        ups = (snap.get("brief") or {}).get("upgrades") or []
        assert len(ups) == 2
        assert all(u.get("done") for u in ups)
        assert set((snap.get("checks") or {})) >= {"1", "2"}
        assert (snap.get("checks") or {}).get("1", {}).get("ok") is True
        assert snap.get("last_check", {}).get("upgrade_id") == snap.get("job_upgrade_id")
        assert snap.get("started_at", 0) > 0
        assert snap.get("deadline", 0) > snap.get("started_at", 0)
        turns = snap.get("turns") or []
        assert len(turns) >= 2
        for row in turns:
            if row.get("kind") == "freeze":
                continue
            c = row.get("commit") or ""
            assert len(c) in {0, 7}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_deck_rejects_dirty_unless_allowed(tmp_path, ns_home):
    settings = Settings(
        mock=True, observe=False, home=ns_home, deck_port=0, max_turns=4, stall_after=4
    )
    httpd = serve_deck(settings, demo=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        base = f"http://{host}:{port}"
        widget = _get(base + "/api/repos")["repos"][0]["path"]
        from pathlib import Path

        (Path(widget) / "scratch.py").write_text("x\n")
        code, body = _post(base + "/api/run", {"path": widget, "mock": True})
        assert code == 409, body
        assert "scratch.py" in (body.get("error") or "")
        code, body = _post(
            base + "/api/run", {"path": widget, "mock": True, "allow_dirty": True}
        )
        assert code == 200, body
        deadline = time.time() + 60
        snap = {}
        while time.time() < deadline:
            snap = _get(base + "/api/status")
            if snap.get("state") in {"done", "halted", "error"}:
                break
            time.sleep(0.25)
        assert snap.get("state") in {"done", "halted"}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_deck_run_refused_while_bag_lock_held(ns_home):
    settings = Settings(mock=True, observe=False, home=ns_home, deck_port=0)
    save_bag(
        ns_home,
        {
            "state": "running",
            "runner_pid": os.getpid(),
            "targets": [{"name": "alpha", "state": "queued"}],
        },
    )
    deck = DeckState(settings)
    snap = deck.snapshot()
    assert (snap.get("bag") or {}).get("state") == "running"
    refused = deck.start_run("/tmp/another-repo", True)
    assert refused["ok"] is False
    assert "bag" in (refused.get("error") or "")
    bag_refused = deck.start_bag(dry=True)
    assert bag_refused["ok"] is False


def test_deck_cmm_and_bag_endpoints(tmp_path, ns_home):
    settings = Settings(
        mock=True, observe=False, home=ns_home, deck_port=0, roots=[tmp_path]
    )
    httpd = serve_deck(settings, demo=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        base = f"http://{host}:{port}"
        bag = _get(base + "/api/bag")
        assert "targets" in bag
        forum = _get(base + "/api/forum")
        assert "nights" in forum
        cmm = _get(base + "/api/cmm")
        assert "histogram" in cmm
        with urllib.request.urlopen(base + "/cmm", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        assert "L0" in html
        assert "fonts.googleapis.com" not in html
        code, body = _post(base + "/api/bag", {"dry": True, "mock": True})
        assert code == 200, body
        assert body.get("ok") is True
        assert body.get("dry") is True
    finally:
        httpd.shutdown()
        httpd.server_close()
