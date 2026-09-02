from __future__ import annotations

import json

import pytest

from nightshift.llm import Writer
from nightshift.models import Brief, FrozenBriefError, Upgrade, clamp_brief_size


def _n(n: int) -> list[Upgrade]:
    return [Upgrade(i, chr(ord("a") + i - 1), "true", [f"{chr(ord('a') + i - 1)}.py"]) for i in range(1, n + 1)]


def _three() -> list[Upgrade]:
    return _n(3)


def test_freeze_clamps_2_to_5():
    with pytest.raises(FrozenBriefError):
        Brief.freeze(_n(1))
    with pytest.raises(FrozenBriefError):
        Brief.freeze(_n(6))
    two = Brief.freeze(_n(2))
    assert len(two.upgrades) == 2
    five = Brief.freeze(_n(5))
    assert len(five.upgrades) == 5


def test_frozen_brief_rejects_fourth_via_add_upgrade():
    brief = Brief.freeze(_three())
    with pytest.raises(FrozenBriefError, match="another"):
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
    with pytest.raises(FrozenBriefError, match="exactly 3"):
        Brief.from_proposed(data)


def test_from_proposed_accepts_size_2():
    data = {
        "upgrades": [
            {"title": "one", "check_command": "true", "paths": ["a"]},
            {"title": "two", "check_command": "true", "paths": ["b"]},
        ]
    }
    brief = Brief.from_proposed(data, size=2)
    assert len(brief.upgrades) == 2
    assert brief.upgrades[0].title == "one"


def test_from_proposed_size_mismatch():
    two = {
        "upgrades": [
            {"title": "one", "check_command": "true", "paths": ["a"]},
            {"title": "two", "check_command": "true", "paths": ["b"]},
        ]
    }
    with pytest.raises(FrozenBriefError, match="exactly 3"):
        Brief.from_proposed(two)
    three = {
        "upgrades": [
            {"title": "one", "check_command": "true", "paths": ["a"]},
            {"title": "two", "check_command": "true", "paths": ["b"]},
            {"title": "three", "check_command": "true", "paths": ["c"]},
        ]
    }
    with pytest.raises(FrozenBriefError, match="exactly 2"):
        Brief.from_proposed(three, size=2)


def test_clamp_brief_size_rejects_out_of_range():
    with pytest.raises(FrozenBriefError):
        clamp_brief_size(1)
    with pytest.raises(FrozenBriefError):
        clamp_brief_size(6)
    assert clamp_brief_size(2) == 2
    assert clamp_brief_size(3) == 3
    assert clamp_brief_size(5) == 5


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
