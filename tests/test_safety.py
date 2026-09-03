from __future__ import annotations

from pathlib import Path

import pytest

from nightshift.gitops import git, rev_parse
from nightshift.models import SafetyError
from nightshift.safety import assert_clean_tree, assert_safe_target, is_nightshift_repo, tree_state


def test_nightshift_repo_detection():
    here = Path(__file__).resolve().parents[1]
    assert is_nightshift_repo(here)
    with pytest.raises(SafetyError, match="own repo"):
        assert_safe_target(here, explicit=False)
    resolved = assert_safe_target(here, explicit=True)
    assert resolved == here.resolve()


def test_clean_tree_rules(fixture_repo):
    (fixture_repo / "README.md").write_text(
        (fixture_repo / "README.md").read_text() + "dirty\n"
    )
    with pytest.raises(SafetyError, match="README.md"):
        assert_clean_tree(fixture_repo)
    git(fixture_repo, "checkout", "--", "README.md")
    (fixture_repo / "scratch.py").write_text("x\n")
    with pytest.raises(SafetyError, match="scratch.py"):
        assert_clean_tree(fixture_repo)
    (fixture_repo / "scratch.py").unlink()
    ns = fixture_repo / ".nightshift"
    ns.mkdir(exist_ok=True)
    (ns / "status.json").write_text("{}\n")
    ts = assert_clean_tree(fixture_repo)
    assert ts.dirty == []
    cache = fixture_repo / "__pycache__"
    cache.mkdir()
    (cache / "x.pyc").write_text("x")
    assert_clean_tree(fixture_repo)
    git_dir = Path(git(fixture_repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    (git_dir / "MERGE_HEAD").write_text("deadbeef\n")
    with pytest.raises(SafetyError, match="MERGE_HEAD"):
        assert_clean_tree(fixture_repo, allow_dirty=True)
    (git_dir / "MERGE_HEAD").unlink()
    git(fixture_repo, "checkout", "--detach")
    with pytest.raises(SafetyError, match="detached HEAD"):
        assert_clean_tree(fixture_repo)
    _ = tree_state
    _ = rev_parse
