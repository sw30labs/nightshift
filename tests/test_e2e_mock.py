from __future__ import annotations

from pathlib import Path

import pytest

from nightshift.gitops import current_branch, rev_parse
from nightshift.models import SafetyError
from nightshift.runner import run_night
from nightshift.safety import assert_safe_target


def test_e2e_mock_remaining_zero(fixture_repo, mock_settings):
    main_sha = rev_parse(fixture_repo, "main")
    report = run_night(fixture_repo, mock_settings, explicit=True)
    assert report.halt_reason == "remaining_zero"
    assert report.remaining_count == 0
    assert report.summary_path.is_file()
    summary = report.summary_path.read_text(encoding="utf-8")
    assert "What changed" in summary
    assert "What the critic refused" in summary
    assert current_branch(fixture_repo).startswith("night/")
    assert rev_parse(fixture_repo, "main") == main_sha
    assert (fixture_repo / ".nightshift" / "brief.json").is_file()
    text = (fixture_repo / "widget.py").read_text(encoding="utf-8")
    assert "def greet" in text
    assert "return a + b" in text
    assert "a + b + 1" not in text


def test_refuse_root_and_home(tmp_path):
    with pytest.raises(SafetyError):
        assert_safe_target(Path("/"), explicit=True)
    with pytest.raises(SafetyError):
        assert_safe_target(Path.home(), explicit=True)
    with pytest.raises(SafetyError):
        assert_safe_target(tmp_path, explicit=True)
