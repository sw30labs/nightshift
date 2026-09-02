from __future__ import annotations

import pytest

from nightshift.gitops import current_branch, rev_parse
from nightshift.llm import Critic, MockChatClient, write_project_file
from nightshift.models import SafetyError
from nightshift.runner import run_night


def test_critic_class_has_no_write_tool():
    forbidden = {"write_file", "write", "edit_file", "apply_patch", "create_file"}
    names = set(dir(Critic))
    assert forbidden.isdisjoint(names)


def test_write_project_file_rejects_non_writer(fixture_repo):
    with pytest.raises(SafetyError, match="only the writer"):
        write_project_file(fixture_repo, "widget.py", "nope\n", role="critic")


def test_minute_zero_does_not_write_project_body(fixture_repo, mock_settings):
    before = (fixture_repo / "widget.py").read_text(encoding="utf-8")
    main_sha = rev_parse(fixture_repo, "main")
    mock_settings.max_turns = 0
    report = run_night(fixture_repo, mock_settings, explicit=True)
    assert (fixture_repo / "widget.py").read_text(encoding="utf-8") == before
    assert (fixture_repo / ".nightshift" / "brief.json").is_file()
    assert current_branch(fixture_repo).startswith("night/")
    assert rev_parse(fixture_repo, "main") == main_sha
    assert report.remaining_count == 3
    critic = Critic(MockChatClient("critic", fixture_repo), fixture_repo)
    assert not hasattr(critic, "write_file")
