from __future__ import annotations

import json
from pathlib import Path

from nightshift.gitops import git
from nightshift.graph import LoopNodes, NightContext, read_snapshot
from nightshift.host import interpreter_for
from nightshift.ledger import save_ledger
from nightshift.llm import Critic, MockChatClient, Writer
from nightshift.models import Brief, Upgrade
from nightshift.status import StatusBoard


def _commit(repo: Path, msg: str = "add") -> None:
    git(repo, "add", "-A")
    git(
        repo,
        "commit",
        "-m",
        msg,
        extra_env={
            "GIT_AUTHOR_NAME": "Nightshift Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@localhost",
            "GIT_COMMITTER_NAME": "Nightshift Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@localhost",
        },
    )


def test_job_files_survive_writer_cut(tmp_path: Path):
    repo = tmp_path / "big"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "n@localhost")
    git(repo, "config", "user.name", "n")
    (repo / "LICENSE").write_text("L" * 130_000)
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("A = 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    (repo / "tests" / "test_y.py").write_text("def test_y():\n    assert True\n")
    (repo / "README.md").write_text("# toy\n")
    (repo / ".env").write_text("SECRET=1\n")
    _commit(repo, "init")
    save_ledger(
        repo,
        {
            "entries": [
                {
                    "title": "prior",
                    "check_command": "true",
                    "paths": ["src/a.py"],
                    "check_hash": "abc",
                    "night": "night/2026-09-01",
                    "attempted": True,
                    "done": True,
                    "voided": False,
                    "void_reason": "",
                    "last_exit": 0,
                    "turns": 1,
                }
            ]
        },
    )
    snap = read_snapshot(repo, focus=["tests/test_x.py"])
    head = snap[:120_000]
    assert "## job file tests/test_x.py" in head
    assert "def test_x()" in head
    assert "L" * 80 not in snap  # LICENSE body skipped
    assert "LICENSE (not shown)" in snap or "LICENSE" in snap.split("## tree", 1)[1].split("## shown")[0]
    shown = snap.split("## shown in full", 1)[1].split("##", 1)[0]
    assert "tests/test_x.py" in shown
    assert "SECRET=1" not in snap
    ledger_at = snap.find("## Prior night ledger")
    file_at = snap.find("## file ")
    assert ledger_at != -1
    if file_at != -1:
        assert ledger_at < file_at
    snap2 = read_snapshot(
        repo, focus=["tests/test_x.py"], check_command="python -m pytest tests/test_y.py -q"
    )
    assert "## file tests/test_y.py" in snap2 or "## job file tests/test_y.py" in snap2
    snap3 = read_snapshot(repo, focus=[".env"])
    assert "SECRET=1" not in snap3
    assert "## job file .env" not in snap3


def test_home_only_ledger_entry_appears_with_home(fixture_repo, ns_home):
    save_ledger(
        fixture_repo,
        {
            "entries": [
                {
                    "title": "quote host checks with shlex",
                    "check_command": "pytest tests/test_host.py -q",
                    "paths": ["host.py"],
                    "check_hash": "abc123abc123",
                    "night": "night/2026-09-01",
                    "attempted": True,
                    "done": True,
                    "voided": False,
                    "void_reason": "",
                    "last_exit": 0,
                    "turns": 1,
                }
            ]
        },
        home=ns_home,
    )
    # the night branch is gone: only the home shard remembers the row
    (fixture_repo / ".nightshift" / "ledger.json").unlink()
    assert not (fixture_repo / ".nightshift" / "ledger.json").exists()
    with_home = read_snapshot(fixture_repo, home=ns_home)
    assert "## Prior night ledger" in with_home
    assert "quote host checks with shlex" in with_home
    without = read_snapshot(fixture_repo)
    assert "## Prior night ledger" not in without
    assert "quote host checks with shlex" not in without
    # forum= is freeze-only and lands in PR 3; passing it must not break the snapshot
    assert "quote host checks with shlex" in read_snapshot(fixture_repo, home=ns_home, forum={})


def test_writer_node_passes_home_not_forum(fixture_repo, mock_settings, ns_home, monkeypatch):
    captured: dict = {}

    def fake_snapshot(repo, **kwargs):
        captured.update(kwargs)
        return "# snap\n"

    monkeypatch.setattr("nightshift.graph.read_snapshot", fake_snapshot)
    brief = Brief.freeze(
        [
            Upgrade(1, "a", "python -m pytest tests/test_widget.py::test_add -q", ["widget.py"]),
            Upgrade(2, "b", "true", ["README.md"]),
        ]
    )
    ctx = NightContext(
        repo=fixture_repo,
        settings=mock_settings,
        writer=Writer(MockChatClient("writer", fixture_repo), fixture_repo),
        critic=Critic(MockChatClient("critic", fixture_repo), fixture_repo),
        status=StatusBoard(ns_home),
        clock=mock_settings.now_fn,
        deadline=mock_settings.now_fn(),
    )
    LoopNodes(ctx).writer(
        {"brief": brief.to_dict(), "job": "x", "job_upgrade_id": 1, "turn": 1, "job_feedback": {}}
    )
    assert captured.get("home") == ns_home
    assert "forum" not in captured


def test_writer_node_passes_focus(fixture_repo, mock_settings, ns_home, monkeypatch):
    captured: dict = {}

    def fake_snapshot(repo, **kwargs):
        captured.update(kwargs)
        captured["repo"] = repo
        return "# snap\n"

    monkeypatch.setattr("nightshift.graph.read_snapshot", fake_snapshot)
    brief = Brief.freeze(
        [
            Upgrade(1, "a", "python -m pytest tests/test_widget.py::test_add -q", ["widget.py"]),
            Upgrade(2, "b", "true", ["README.md"]),
        ]
    )
    board = StatusBoard(ns_home)
    ctx = NightContext(
        repo=fixture_repo,
        settings=mock_settings,
        writer=Writer(MockChatClient("writer", fixture_repo), fixture_repo),
        critic=Critic(MockChatClient("critic", fixture_repo), fixture_repo),
        status=board,
        clock=mock_settings.now_fn,
        deadline=mock_settings.now_fn(),
    )
    nodes = LoopNodes(ctx)
    nodes.writer(
        {
            "brief": brief.to_dict(),
            "job": "Make test_add pass",
            "job_upgrade_id": 1,
            "turn": 1,
            "job_feedback": {},
        }
    )
    assert captured.get("focus") == ["widget.py"]
    assert "test_widget.py" in str(captured.get("check_command") or "")
    _ = interpreter_for
    _ = json
