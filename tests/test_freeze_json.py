from __future__ import annotations

import json

import pytest

from nightshift.llm import Critic, completion_text, parse_json_object
from nightshift.models import FrozenBriefError


def test_parse_json_object_fenced_and_prose():
    assert parse_json_object('{"a": 1}')["a"] == 1
    assert parse_json_object("```json\n{\"a\": 1}\n```")["a"] == 1
    assert parse_json_object("sure.\n{\"a\": 1}\nthanks")["a"] == 1


def test_parse_json_object_empty_raises():
    with pytest.raises(ValueError, match="no JSON object"):
        parse_json_object("")
    with pytest.raises(ValueError, match="no JSON object"):
        parse_json_object("no braces here")


def test_completion_text_falls_back_to_reasoning_content():
    empty = {
        "choices": [
            {"message": {"role": "assistant", "content": "", "reasoning_content": '{"ok": true}'}}
        ]
    }
    assert json.loads(completion_text(empty))["ok"] is True
    filled = {
        "choices": [
            {"message": {"role": "assistant", "content": '{"from": "content"}', "reasoning_content": '{"from": "reason"}'}}
        ]
    }
    assert json.loads(completion_text(filled))["from"] == "content"


def test_propose_brief_retries_then_succeeds(fixture_repo):
    class Flaky:
        mock = False
        n = 0

        def chat(self, messages, **kwargs):
            self.n += 1
            if self.n < 3:
                return "thinking in prose, no json"
            return json.dumps(
                {
                    "upgrades": [
                        {"title": "one", "check_command": "true", "paths": ["a.py"]},
                        {"title": "two", "check_command": "true", "paths": ["b.py"]},
                        {"title": "three", "check_command": "true", "paths": ["c.py"]},
                    ]
                }
            )

    client = Flaky()
    upgrades = Critic(client, fixture_repo).propose_brief("snapshot", size=3)
    assert client.n == 3
    assert len(upgrades) == 3
    assert upgrades[0].title == "one"


def test_propose_brief_still_fatal_after_retries(fixture_repo):
    class Dead:
        mock = False

        def chat(self, messages, **kwargs):
            return "still thinking"

    with pytest.raises(ValueError, match="after 3 freeze attempts"):
        Critic(Dead(), fixture_repo).propose_brief("snapshot")


def test_propose_brief_does_not_retry_a_fourth_upgrade(fixture_repo):
    class Four:
        mock = False
        n = 0

        def chat(self, messages, **kwargs):
            self.n += 1
            return json.dumps(
                {
                    "upgrades": [
                        {"title": "one", "check_command": "true", "paths": ["a.py"]},
                        {"title": "two", "check_command": "true", "paths": ["b.py"]},
                        {"title": "three", "check_command": "true", "paths": ["c.py"]},
                        {"title": "four", "check_command": "true", "paths": ["d.py"]},
                    ]
                }
            )

    client = Four()
    with pytest.raises(FrozenBriefError, match="exactly 3"):
        Critic(client, fixture_repo).propose_brief("snapshot", size=3)
    assert client.n == 1
