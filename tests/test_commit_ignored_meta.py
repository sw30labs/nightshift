from __future__ import annotations

from nightshift.gitops import checkout_night_branch, commit_paths, git
from datetime import datetime


def test_commit_paths_force_adds_gitignored_nightshift_meta(fixture_repo):
    gitignore = fixture_repo / ".gitignore"
    gitignore.write_text(gitignore.read_text(encoding="utf-8") + ".nightshift/\n", encoding="utf-8")
    git(fixture_repo, "add", "--", ".gitignore")
    git(
        fixture_repo,
        "commit",
        "-m",
        "ignore .nightshift",
        extra_env={
            "GIT_AUTHOR_NAME": "Nightshift Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@localhost",
            "GIT_COMMITTER_NAME": "Nightshift Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@localhost",
        },
    )
    checkout_night_branch(fixture_repo, datetime(2026, 9, 2, 12, 40))
    meta = fixture_repo / ".nightshift" / "brief.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text('{"frozen": true, "upgrades": []}\n', encoding="utf-8")
    sha = commit_paths(
        fixture_repo,
        "nightshift: freeze brief (5 upgrades)",
        [".nightshift/brief.json"],
    )
    assert sha
    tracked = git(fixture_repo, "ls-files", "--", ".nightshift/brief.json")
    assert tracked.stdout.strip() == ".nightshift/brief.json"


def test_commit_paths_does_not_force_add_unscoped_ignored(fixture_repo):
    gitignore = fixture_repo / ".gitignore"
    gitignore.write_text(gitignore.read_text(encoding="utf-8") + "secret.bin\n", encoding="utf-8")
    git(fixture_repo, "add", "--", ".gitignore")
    git(
        fixture_repo,
        "commit",
        "-m",
        "ignore secret.bin",
        extra_env={
            "GIT_AUTHOR_NAME": "Nightshift Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@localhost",
            "GIT_COMMITTER_NAME": "Nightshift Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@localhost",
        },
    )
    checkout_night_branch(fixture_repo, datetime(2026, 9, 2, 12, 41))
    (fixture_repo / "secret.bin").write_bytes(b"nope")
    (fixture_repo / "ok.txt").write_text("yes\n", encoding="utf-8")
    sha = commit_paths(fixture_repo, "nightshift: turn 1 — ok", None)
    assert sha
    names = git(fixture_repo, "ls-files", "--", "secret.bin", "ok.txt").stdout.split()
    assert "ok.txt" in names
    assert "secret.bin" not in names


def test_commit_paths_stages_deletion(fixture_repo):
    checkout_night_branch(fixture_repo, datetime(2026, 9, 2, 12, 42))
    git(fixture_repo, "rm", "--", "README.md")
    sha = commit_paths(fixture_repo, "nightshift: drop readme", None)
    assert sha
    names = git(fixture_repo, "ls-files", "--", "README.md").stdout.strip()
    assert names == ""
