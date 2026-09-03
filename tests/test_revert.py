from __future__ import annotations

from nightshift.gitops import changed_paths, revert_paths
from nightshift.graph import unapproved_paths
from nightshift.llm import write_project_file


def test_revert_unapproved_paths(fixture_repo):
    write_project_file(
        fixture_repo, "widget.py", "def add(a, b):\n    return a + b\n", role="writer"
    )
    write_project_file(fixture_repo, "gold.md", "# extra essay\n", role="writer")
    changed = changed_paths(fixture_repo)
    assert "gold.md" in changed
    allowed = {"widget.py"}
    bad = unapproved_paths(changed, allowed)
    assert "gold.md" in bad
    assert "widget.py" not in bad
    reverted = revert_paths(fixture_repo, bad)
    assert "gold.md" in reverted
    assert not (fixture_repo / "gold.md").exists()
    assert "return a + b" in (fixture_repo / "widget.py").read_text(encoding="utf-8")


def test_revert_paths_skips_absolute_escape(fixture_repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    reverted = revert_paths(fixture_repo, [str(outside)])
    assert any(x.startswith("SKIP (outside repo):") for x in reverted)
    assert outside.exists()
    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_revert_paths_skips_parent_escape(fixture_repo):
    outside = fixture_repo.parent / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    reverted = revert_paths(fixture_repo, ["../outside.txt"])
    assert any(x.startswith("SKIP (outside repo):") for x in reverted)
    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_revert_untracked_file_in_new_directory(fixture_repo):
    target = fixture_repo / "tests" / "new" / "x.py"
    target.parent.mkdir(parents=True)
    write_project_file(fixture_repo, "tests/new/x.py", "x = 1\n", role="writer")
    changed = changed_paths(fixture_repo)
    assert "tests/new/x.py" in changed
    reverted = revert_paths(fixture_repo, ["tests/new/x.py"])
    assert "tests/new/x.py" in reverted
    assert not target.exists()
