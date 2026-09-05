from datetime import datetime

import pytest

from nightshift.gitops import changed_paths, checkout_night_branch, commit_paths, commits_touching, diff_stat_against, git, revert_paths
from nightshift.models import SafetyError
from nightshift.safety import assert_inside_repo, git_visible_files, normalize_job_rel


def _night(repo):
    checkout_night_branch(repo, datetime(2026, 9, 4, 12, 0))


def _staged(repo):
    return git(repo, "diff", "--cached", "--binary").stdout


def test_scoped_commit_preserves_unrelated_staged_content(fixture_repo):
    _night(fixture_repo)
    readme = fixture_repo / "README.md"
    original = readme.read_text()
    readme.write_text(original + "user staged content\n")
    git(fixture_repo, "add", "--", "README.md")
    staged = _staged(fixture_repo)
    readme.write_text(readme.read_text() + "user unstaged content\n")
    (fixture_repo / "result.txt").write_text("nightshift result\n")

    assert commit_paths(fixture_repo, "scoped result", ["result.txt"])

    assert _staged(fixture_repo) == staged
    assert git(fixture_repo, "show", "HEAD:README.md").stdout == original
    assert "user unstaged content" in readme.read_text()
    assert git(fixture_repo, "show", "HEAD:result.txt").stdout == "nightshift result\n"


def test_automatic_commit_respects_staged_exclusions_and_secrets(fixture_repo):
    _night(fixture_repo)
    (fixture_repo / "README.md").write_text("user work\n")
    (fixture_repo / ".env").write_text("TOKEN=private\n")
    git(fixture_repo, "add", "-f", "--", "README.md", ".env")
    staged = _staged(fixture_repo)
    (fixture_repo / "result.txt").write_text("nightshift result\n")

    assert commit_paths(fixture_repo, "safe result", exclude={"README.md"})

    assert _staged(fixture_repo) == staged
    assert git(fixture_repo, "cat-file", "-e", "HEAD:.env", check=False).returncode != 0
    assert git(fixture_repo, "show", "HEAD:README.md").stdout != "user work\n"


@pytest.mark.parametrize("paths", [[], ["widget.py"]])
def test_noop_commit_preserves_user_index(fixture_repo, paths):
    _night(fixture_repo)
    (fixture_repo / "README.md").write_text("user work\n")
    git(fixture_repo, "add", "--", "README.md")
    staged = _staged(fixture_repo)

    assert commit_paths(fixture_repo, "nothing to commit", paths) is None
    assert _staged(fixture_repo) == staged


def test_automatic_noop_preserves_excluded_index(fixture_repo):
    _night(fixture_repo)
    (fixture_repo / "README.md").write_text("user work\n")
    git(fixture_repo, "add", "--", "README.md")
    staged = _staged(fixture_repo)

    assert commit_paths(fixture_repo, "nothing to commit", exclude={"README.md"}) is None
    assert _staged(fixture_repo) == staged


def test_scoped_directory_filters_nested_excluded_and_secret_files(fixture_repo):
    _night(fixture_repo)
    folder = fixture_repo / "new"
    folder.mkdir()
    (folder / "good.txt").write_text("keep\n")
    (folder / "user.txt").write_text("user\n")
    (folder / ".env").write_text("TOKEN=private\n")

    assert commit_paths(fixture_repo, "safe directory", ["new"], exclude={"new/user.txt"})
    assert git(fixture_repo, "ls-tree", "-r", "--name-only", "HEAD", "--", "new").stdout.splitlines() == ["new/good.txt"]


def test_revert_untracked_symlink_preserves_its_target(fixture_repo):
    target = fixture_repo / "README.md"
    original = target.read_text()
    link = fixture_repo / "alias.txt"
    link.symlink_to(target)

    assert revert_paths(fixture_repo, ["alias.txt"]) == ["alias.txt"]
    assert not link.is_symlink()
    assert target.read_text() == original


def test_revert_outside_and_dangling_symlinks_only_removes_link(fixture_repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n")
    (fixture_repo / "outside-link").symlink_to(outside)
    (fixture_repo / "dangling-link").symlink_to(tmp_path / "missing.txt")

    assert revert_paths(fixture_repo, ["outside-link", "dangling-link"]) == ["outside-link", "dangling-link"]
    assert outside.read_text() == "keep\n"
    assert not (fixture_repo / "outside-link").is_symlink()
    assert not (fixture_repo / "dangling-link").is_symlink()


def test_revert_restores_head_including_staged_deletions(fixture_repo):
    original = (fixture_repo / "README.md").read_text()
    git(fixture_repo, "rm", "--", "README.md")

    assert revert_paths(fixture_repo, ["README.md"]) == ["README.md"]
    assert (fixture_repo / "README.md").read_text() == original
    assert _staged(fixture_repo) == ""


def test_revert_removes_new_staged_file(fixture_repo):
    (fixture_repo / "extra.txt").write_text("extra\n")
    git(fixture_repo, "add", "--", "extra.txt")

    assert revert_paths(fixture_repo, ["extra.txt"]) == ["extra.txt"]
    assert not (fixture_repo / "extra.txt").exists()
    assert _staged(fixture_repo) == ""


def test_revert_literal_wildcard_does_not_revert_other_files(fixture_repo):
    readme = fixture_repo / "README.md"
    readme.write_text("user work\n")
    (fixture_repo / "*.md").write_text("extra\n")

    assert revert_paths(fixture_repo, ["*.md"]) == ["*.md"]
    assert readme.read_text() == "user work\n"


def test_scoped_commit_literal_wildcard_excludes_matching_names(fixture_repo):
    _night(fixture_repo)
    (fixture_repo / "*.txt").write_text("selected\n")
    (fixture_repo / "other.txt").write_text("user work\n")

    assert commit_paths(fixture_repo, "literal name", ["*.txt"])
    assert git(fixture_repo, "show", "HEAD:*.txt").stdout == "selected\n"
    assert git(fixture_repo, "cat-file", "-e", "HEAD:other.txt", check=False).returncode != 0


def test_git_paths_preserve_whitespace_newlines_unicode_and_arrows(fixture_repo):
    names = [" leading.txt", "trailing.txt ", "line\nbreak.txt", "café.txt", "a -> b.txt"]
    for name in names:
        (fixture_repo / name).write_text("new\n")

    assert set(names) <= set(changed_paths(fixture_repo))
    assert set(names) <= set(git_visible_files(fixture_repo))


def test_changed_paths_reports_both_rename_endpoints(fixture_repo):
    git(fixture_repo, "mv", "README.md", "guide.md")
    assert {"README.md", "guide.md"} <= set(changed_paths(fixture_repo))


@pytest.mark.parametrize("rel", [".", "./", "../README.md", "/tmp/outside", ".ENV", ".env/private.txt"])
def test_writer_rejects_invalid_or_protected_paths(fixture_repo, rel):
    with pytest.raises(SafetyError):
        assert_inside_repo(fixture_repo, rel)


def test_writer_rejects_symlink_alias_to_another_job_file(fixture_repo):
    (fixture_repo / "alias.py").symlink_to(fixture_repo / "widget.py")
    with pytest.raises(SafetyError, match="symlink"):
        assert_inside_repo(fixture_repo, "alias.py")


def test_job_relative_paths_collapse_dot_segments():
    assert normalize_job_rel("src/./widget.py") == "src/widget.py"


@pytest.mark.parametrize("paths", [None, ["README.md"]])
def test_staged_deletion_commit_preserves_other_staged_user_work(fixture_repo, paths):
    _night(fixture_repo)
    (fixture_repo / "widget.py").write_text("user work\n")
    git(fixture_repo, "add", "--", "widget.py")
    user_staged = _staged(fixture_repo)
    git(fixture_repo, "rm", "--", "README.md")

    assert commit_paths(fixture_repo, "remove readme", paths, exclude={"widget.py"})
    assert _staged(fixture_repo) == user_staged
    assert git(fixture_repo, "cat-file", "-e", "HEAD:README.md", check=False).returncode != 0


def test_summary_diff_keeps_intentional_git_pathspec_exclusions(fixture_repo):
    _night(fixture_repo)
    base = git(fixture_repo, "rev-parse", "HEAD").stdout.strip()
    (fixture_repo / "widget.py").write_text("# project change\n")
    meta = fixture_repo / ".nightshift" / "brief.json"
    meta.parent.mkdir(exist_ok=True)
    meta.write_text("{}\n")
    assert commit_paths(fixture_repo, "project and brief", ["widget.py", ".nightshift/brief.json"])

    stat = diff_stat_against(fixture_repo, base, extra=["--", ".", ":!.nightshift"])
    assert "widget.py" in stat
    assert ".nightshift" not in stat


def test_commits_touching_treats_wildcard_filename_literally(fixture_repo):
    _night(fixture_repo)
    (fixture_repo / "*.txt").write_text("literal file\n")
    (fixture_repo / "other.txt").write_text("ordinary file\n")
    assert commit_paths(fixture_repo, "seed names", ["*.txt", "other.txt"])
    base = git(fixture_repo, "rev-parse", "HEAD").stdout.strip()
    (fixture_repo / "other.txt").write_text("ordinary change\n")
    assert commit_paths(fixture_repo, "change ordinary file", ["other.txt"])

    assert commits_touching(fixture_repo, base, ["*.txt"]) == []
    assert len(commits_touching(fixture_repo, base, ["other.txt"])) == 1
