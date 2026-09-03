from __future__ import annotations

from nightshift.gitops import git
from nightshift.runner import run_night
from nightshift.safety import is_junk


def test_is_junk_meta_and_caches():
    assert is_junk(".nightshift/history/20260903-010101/events.jsonl") is True
    assert is_junk("src/.DS_Store") is True
    assert is_junk(".nightshift/status.json") is True
    assert is_junk("widget.py") is False


def test_mock_night_does_not_commit_junk(tmp_path, mock_settings):
    from nightshift.demo import seed_widget

    repo = seed_widget(tmp_path / "widget")
    (repo / ".gitignore").write_text("")  # empty, so pyc would otherwise be addable
    git(repo, "add", "--", ".gitignore")
    git(
        repo,
        "commit",
        "-m",
        "empty gitignore",
        extra_env={
            "GIT_AUTHOR_NAME": "Nightshift Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@localhost",
            "GIT_COMMITTER_NAME": "Nightshift Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@localhost",
        },
    )
    report = run_night(repo, mock_settings, explicit=True)
    names = git(repo, "ls-files").stdout
    assert ".pyc" not in names
    assert ".pytest_cache" not in names
    assert "history/" not in names
    assert ".nightshift/status.json" not in names
    porcelain = git(repo, "status", "--porcelain").stdout
    leftover = [
        ln
        for ln in porcelain.splitlines()
        if ln.strip()
        and ".nightshift/" not in ln
        and "__pycache__" not in ln
        and ".pytest_cache" not in ln
    ]
    assert leftover == [], leftover
    assert report.halt_reason == "remaining_zero"
