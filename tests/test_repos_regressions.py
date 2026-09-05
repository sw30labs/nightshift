from nightshift.gitops import git
from nightshift.repos import _quick_status, find_repos


def test_nested_nightshift_directory_is_real_project_dirt(fixture_repo):
    nested = fixture_repo / "src" / ".nightshift" / "config.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("value = 1\n")

    assert _quick_status(fixture_repo)[1] is True
    assert find_repos([fixture_repo])[0].dirty is True


def test_runtime_metadata_and_junk_do_not_report_dirty(fixture_repo):
    meta = fixture_repo / ".nightshift" / "status.json"
    meta.parent.mkdir(exist_ok=True)
    meta.write_text("{}\n")
    cache = fixture_repo / "__pycache__" / "module.pyc"
    cache.parent.mkdir(exist_ok=True)
    cache.write_bytes(b"cache")

    assert _quick_status(fixture_repo) == ("main", False)


def test_new_repository_reports_its_unborn_branch(tmp_path):
    git(tmp_path, "init", "-b", "fresh")
    assert _quick_status(tmp_path) == ("fresh", False)


def test_broken_git_metadata_is_not_reported_as_clean(tmp_path):
    (tmp_path / ".git").write_text("not a valid git worktree\n")
    assert _quick_status(tmp_path) == ("?", True)


def test_rename_from_project_into_metadata_is_still_dirty(fixture_repo):
    (fixture_repo / ".nightshift").mkdir(exist_ok=True)
    git(fixture_repo, "mv", "README.md", ".nightshift/readme.md")
    assert _quick_status(fixture_repo)[1] is True
