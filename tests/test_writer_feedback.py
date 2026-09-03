from __future__ import annotations

import json
from pathlib import Path

from nightshift.graph import LoopNodes, NightContext, apply_host_truth
from nightshift.llm import Critic, MockChatClient, Writer
from nightshift.models import Brief, CheckResult, Upgrade, WriterResult
from nightshift.status import StatusBoard


def _brief(paths=("widget.py",)):
    return Brief.freeze(
        [
            Upgrade(1, "a", "true", list(paths)),
            Upgrade(2, "b", "true", ["README.md"]),
        ]
    )


def test_feedback_block_before_snapshot(fixture_repo):
    class Capture:
        mock = False
        seen = ""

        def chat(self, messages, **kwargs):
            self.seen = messages[1]["content"]
            return json.dumps({"files": [], "message": "ok"})

    client = Capture()
    feedback = {
        "upgrade_id": 1,
        "turn": 3,
        "command": "pytest -q",
        "exit_code": 2,
        "output": "FAILED tests/test_widget.py::test_add",
        "writer_refused": ["widget.py: patch hunk not found"],
        "critic_notes": ["try again"],
    }
    Writer(client, fixture_repo).apply_job(
        "fix add", _brief(), "Repo snapshot here", feedback=feedback
    )
    text = client.seen
    assert "Previous attempt (turn 3) failed" in text
    assert "FAILED tests/test_widget.py::test_add" in text
    assert "widget.py: patch hunk not found" in text
    assert text.index("Previous attempt") < text.index("Repo snapshot")
    Writer(client, fixture_repo).apply_job(
        "fix add",
        _brief(),
        "Repo snapshot here",
        feedback={**feedback, "upgrade_id": 2},
    )
    assert "Previous attempt" not in client.seen.split("Current job paths", 1)[-1]


def test_job_line_includes_last_attempt(fixture_repo):
    class Capture:
        mock = False
        payload = None

        def chat(self, messages, **kwargs):
            self.payload = json.loads(messages[-1]["content"])
            return json.dumps({"upgrade_id": 99, "job": "fix the failing test_add"})

    client = Capture()
    uid, job = Critic(client, fixture_repo).job_line(
        _brief(),
        feedback={
            "upgrade_id": 1,
            "turn": 2,
            "exit_code": 1,
            "output": "x" * 50 + "TAIL_MARKER",
            "writer_refused": ["nope"],
            "critic_notes": ["note"],
        },
    )
    assert uid == 1
    assert "last_attempt" in client.payload
    assert client.payload["last_attempt"]["output_tail"].endswith("TAIL_MARKER")
    assert job == "fix the failing test_add"


def test_critic_score_feedback_and_notes(fixture_repo, mock_settings, ns_home, monkeypatch):
    brief = Brief.freeze(
        [
            Upgrade(1, "add", "false", ["widget.py"]),
            Upgrade(2, "greet", "true", ["widget.py"]),
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
        base_sha="HEAD",
    )
    nodes = LoopNodes(ctx)
    monkeypatch.setattr(
        "nightshift.graph.commit_paths", lambda *a, **k: "abcdef1"
    )
    out = nodes.critic_score(
        {
            "brief": brief.to_dict(),
            "job_upgrade_id": 1,
            "job": "fix add",
            "turn": 2,
            "check_results": [
                {
                    "upgrade_id": 1,
                    "command": "false",
                    "ok": False,
                    "exit_code": 1,
                    "output": "ModuleNotFoundError: No module named 'x'\n",
                }
            ],
            "last_diff": "",
            "check_logs": "fail",
            "turn_refused": ["hunk missed"],
            "written": ["widget.py"],
            "job_base": {},
            "compile_errors": [],
            "job_red_ids": {},
            "fail_streak": {},
        }
    )
    fb = out["job_feedback"]
    assert fb["upgrade_id"] == 1
    assert fb["exit_code"] == 1
    assert "hunk missed" in fb["writer_refused"]
    assert Brief.from_dict(out["brief"]).upgrades[0].note.startswith("turn ")

    brief2 = Brief.freeze(
        [
            Upgrade(1, "add", "true", ["widget.py"]),
            Upgrade(2, "greet", "true", ["README.md"]),
        ]
    )
    apply_host_truth(
        brief2,
        [CheckResult(1, "true", True, 0, "1 passed"), CheckResult(2, "true", True, 0, "")],
        job_id=1,
        night_changed={"widget.py"},
    )
    assert brief2.upgrades[0].done is True


def test_mock_ignores_greet_after_job_paths(fixture_repo):
    user = (
        "Current job:\nMake test_add pass\n\n"
        "Current job paths[] (writes outside these are refused): [\"widget.py\"]\n\n"
        "test_greet lives in the snapshot\n"
        "Repo snapshot:\nnothing\n"
    )
    payload = MockChatClient("writer", fixture_repo)._writer_payload(user)
    text = payload["files"][0]["content"]
    assert "def greet" not in text
    assert "a + b" in text


def test_degenerate_and_closest_and_truncation(fixture_repo):
    readme = fixture_repo / "README.md"
    readme.write_text("# Demo\nRun with -p 8000:8000\nport 8000\n")

    class Degenerate:
        mock = False

        def chat(self, messages, **kwargs):
            return json.dumps(
                {
                    "patches": [{"path": "README.md", "old": " ", "new": "x"}],
                    "files": [],
                    "message": "nope",
                }
            )

    result = Writer(Degenerate(), fixture_repo).apply_job("x", _brief(["README.md"]), "")
    assert result.written == []
    assert any("degenerate" in n for n in result.refused)

    class Miss:
        mock = False

        def chat(self, messages, **kwargs):
            return json.dumps(
                {
                    "patches": [
                        {"path": "README.md", "old": "Run with -p 8000:8001\n", "new": "ok\n"}
                    ],
                    "files": [],
                    "message": "nope",
                }
            )

    result = Writer(Miss(), fixture_repo).apply_job("x", _brief(["README.md"]), "")
    assert any("closest line" in n for n in result.refused)

    class Once:
        mock = False
        n = 0
        last_finish_reason = ""

        def chat(self, messages, **kwargs):
            self.n += 1
            if self.n == 1:
                self.last_finish_reason = "length"
                return "partial { not json"
            self.last_finish_reason = "stop"
            return json.dumps(
                {
                    "files": [
                        {"path": "widget.py", "content": "def add(a, b):\n    return a + b\n"}
                    ],
                    "message": "ok",
                }
            )

    r = Writer(Once(), fixture_repo).apply_job("fix", _brief(["widget.py"]), "")
    assert "widget.py" in r.written
    assert not any("non-JSON" in n for n in r.refused)

    class Always:
        mock = False
        last_finish_reason = "length"

        def chat(self, messages, **kwargs):
            self.last_finish_reason = "length"
            return "still cut off"

    r = Writer(Always(), fixture_repo).apply_job("fix", _brief(["widget.py"]), "")
    assert r.written == []
    assert any("truncated" in n for n in r.refused)
    _ = WriterResult
    _ = Path
