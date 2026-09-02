from __future__ import annotations

import json

import pytest

from nightshift.llm import Writer
from nightshift.models import Brief, FrozenBriefError, Upgrade


def _three() -> list[Upgrade]:
    return [
        Upgrade(1, "a", "true", ["a.py"]),
        Upgrade(2, "b", "true", ["b.py"]),
        Upgrade(3, "c", "true", ["c.py"]),
    ]


def test_freeze_requires_exactly_three():
    with pytest.raises(FrozenBriefError):
        Brief.freeze(_three()[:2])
    with pytest.raises(FrozenBriefError):
        Brief.freeze(_three() + [Upgrade(4, "d", "true", ["d.py"])])


def test_frozen_brief_rejects_fourth_via_add_upgrade():
    brief = Brief.freeze(_three())
    with pytest.raises(FrozenBriefError, match="fourth"):
        brief.add_upgrade(Upgrade(4, "gold plate", "true", ["extra.py"]))
    assert brief.remaining_count == 3
    assert len(brief.upgrades) == 3


def test_from_proposed_rejects_fourth():
    data = {
        "upgrades": [
            {"title": "one", "check_command": "true", "paths": ["a"]},
            {"title": "two", "check_command": "true", "paths": ["b"]},
            {"title": "three", "check_command": "true", "paths": ["c"]},
            {"title": "four", "check_command": "true", "paths": ["d"]},
        ]
    }
    with pytest.raises(FrozenBriefError, match="fourth"):
        Brief.from_proposed(data)


def test_writer_payload_cannot_extend_frozen_brief(fixture_repo):
    brief = Brief.freeze(_three(), repo=str(fixture_repo), branch="night/test")

    class FourthClient:
        mock = False

        def chat(self, messages, **kwargs):
            return json.dumps(
                {
                    "files": [{"path": "a.py", "content": "ok\n"}],
                    "upgrades": [
                        {
                            "title": "sneak a fourth",
                            "check_command": "true",
                            "paths": ["x"],
                        }
                    ],
                }
            )

    writer = Writer(FourthClient(), fixture_repo)
    writer.apply_job("do a", brief, "snapshot")
    assert len(brief.upgrades) == 3
    with pytest.raises(FrozenBriefError):
        brief.add_upgrade(Upgrade(4, "no", "true", ["x"]))
