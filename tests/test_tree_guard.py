from __future__ import annotations

import json

from nightshift.graph import LoopNodes, NightContext
from nightshift.llm import Critic, MockChatClient, Writer
from nightshift.models import Brief, Upgrade
from nightshift.status import StatusBoard


def test_syntax_error_reverts_tracked_and_unlinks_untracked(
    fixture_repo, mock_settings, ns_home
):
    before = (fixture_repo / "widget.py").read_text()

    class Broken:
        mock = False

        def chat(self, messages, **kwargs):
            user = messages[1]["content"]
            if "widget.py" in user and "Current job paths" in user:
                return json.dumps(
                    {
                        "files": [{"path": "widget.py", "content": "def (:\n"}],
                        "message": "broken",
                    }
                )
            return json.dumps({"files": [], "message": "ok"})

    brief = Brief.freeze(
        [
            Upgrade(1, "a", "true", ["widget.py"]),
            Upgrade(2, "b", "true", ["tests/test_new.py"]),
        ]
    )
    board = StatusBoard(ns_home)
    ctx = NightContext(
        repo=fixture_repo,
        settings=mock_settings,
        writer=Writer(Broken(), fixture_repo),
        critic=Critic(MockChatClient("critic", fixture_repo), fixture_repo),
        status=board,
        clock=mock_settings.now_fn,
        deadline=mock_settings.now_fn(),
    )
    out = LoopNodes(ctx).writer(
        {
            "brief": brief.to_dict(),
            "job": "fix widget",
            "job_upgrade_id": 1,
            "job_feedback": {},
        }
    )
    assert out["written"] == []
    assert any("SyntaxError" in n and "write reverted" in n for n in out["turn_refused"])
    assert (fixture_repo / "widget.py").read_text() == before

    class NewBroken:
        mock = False

        def chat(self, messages, **kwargs):
            return json.dumps(
                {
                    "files": [{"path": "tests/test_new.py", "content": "def (:\n"}],
                    "message": "broken",
                }
            )

    ctx.writer = Writer(NewBroken(), fixture_repo)
    out = LoopNodes(ctx).writer(
        {
            "brief": brief.to_dict(),
            "job": "add test",
            "job_upgrade_id": 2,
            "job_feedback": {},
        }
    )
    assert out["written"] == []
    assert not (fixture_repo / "tests" / "test_new.py").exists()
