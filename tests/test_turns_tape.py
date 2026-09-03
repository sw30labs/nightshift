from __future__ import annotations

import json

from nightshift import cli
from nightshift.gitops import git
from nightshift.graph import LoopNodes, NightContext, load_turns
from nightshift.llm import Critic, MockChatClient, Writer
from nightshift.models import Brief, Upgrade, WriterResult
from nightshift.runner import run_night
from nightshift.status import StatusBoard


def test_turns_jsonl_after_mock_night(fixture_repo, mock_settings):
    report = run_night(fixture_repo, mock_settings, explicit=True)
    rows = load_turns(fixture_repo)
    assert rows[0].get("kind") == "freeze"
    rest = [r for r in rows if r.get("kind") != "freeze"]
    turns = [r["turn"] for r in rest]
    assert turns == sorted(turns)
    assert turns == list(range(turns[0], turns[0] + len(turns)))
    shas = git(fixture_repo, "log", "--format=%h", f"main..{report.branch}").stdout.split()
    tape_shas = [r.get("commit") for r in rest if r.get("commit")]
    for sha in tape_shas:
        assert len(sha) == 7
        assert any(s.startswith(sha) or sha.startswith(s) for s in shas)
    tracked = git(fixture_repo, "ls-files", "--", ".nightshift/turns.jsonl").stdout.strip()
    assert tracked == ".nightshift/turns.jsonl"


def test_turn_refused_is_per_turn(fixture_repo, mock_settings, ns_home, monkeypatch):
    class Once:
        n = 0
        mock = False

        def apply_job(self, *a, **k):
            self.n += 1
            if self.n == 1:
                return WriterResult(written=[], message="nope", refused=["hunk missed"])
            return Writer(MockChatClient("writer", fixture_repo), fixture_repo).apply_job(*a, **k)

    # Drive via run_night with a writer that refuses first
    monkeypatch.setattr("nightshift.runner.Writer", lambda *a, **k: Once())
    mock_settings.max_turns = 8
    mock_settings.stall_after = 20
    run_night(fixture_repo, mock_settings, explicit=True)
    rows = [r for r in load_turns(fixture_repo) if r.get("kind") != "freeze"]
    assert rows
    assert "hunk missed" in (rows[0].get("writer_refused") or [])
    if len(rows) > 1:
        assert "hunk missed" not in (rows[1].get("writer_refused") or [])


def test_writer_timeout_still_rows(fixture_repo, mock_settings, ns_home):
    class Boom:
        mock = False

        def apply_job(self, *a, **k):
            raise TimeoutError("timed out")

    board = StatusBoard(ns_home)
    ctx = NightContext(
        repo=fixture_repo,
        settings=mock_settings,
        writer=Boom(),  # type: ignore[arg-type]
        critic=Critic(MockChatClient("critic", fixture_repo), fixture_repo),
        status=board,
        clock=mock_settings.now_fn,
        deadline=mock_settings.now_fn(),
    )
    brief = Brief.freeze(
        [Upgrade(1, "a", "true", ["widget.py"]), Upgrade(2, "b", "true", ["README.md"])]
    )
    out = LoopNodes(ctx).writer(
        {"brief": brief.to_dict(), "job": "x", "job_upgrade_id": 1, "job_feedback": {}}
    )
    assert out["written"] == []
    assert any("timed out" in n for n in out["turn_refused"])


def test_turns_cli(tmp_path, fixture_repo, ns_home, capsys):
    tape = fixture_repo / ".nightshift" / "turns.jsonl"
    tape.parent.mkdir(exist_ok=True)
    rows = [
        {"turn": 1, "upgrade_id": 1, "written": [], "check": {"exit_code": 2, "fingerprint": "2:x", "ok": False}, "reverted": [], "commit": "abc1234", "secs": {"writer": 1}},
        {"turn": 2, "upgrade_id": 1, "written": [], "check": {"exit_code": 2, "fingerprint": "2:x", "ok": False}, "reverted": [], "commit": "abc1235", "secs": {"writer": 1}},
        {"turn": 3, "upgrade_id": 1, "written": [], "check": {"exit_code": 2, "fingerprint": "2:x", "ok": False}, "reverted": [], "commit": "abc1236", "secs": {"writer": 1}},
    ]
    tape.write_text("".join(json.dumps(r) + "\n" for r in rows))
    code = cli.main(["turns", str(fixture_repo)])
    assert code == 0
    out = capsys.readouterr().out
    assert "T1" in out and "T3" in out
    assert "stalls:" in out
    assert "same failure x3" in out
    code = cli.main(["turns", str(fixture_repo), "--branch", "main"])
    assert code == 1
