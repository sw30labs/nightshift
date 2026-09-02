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
