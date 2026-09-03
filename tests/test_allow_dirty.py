from __future__ import annotations

from nightshift.gitops import git
from nightshift.runner import run_night


def test_allow_dirty_keeps_wip_out_of_night(fixture_repo, mock_settings):
    readme = fixture_repo / "README.md"
    original = readme.read_text()
    readme.write_text(original + "\nUSER WIP\n")
    scratch = fixture_repo / "scratch.py"
    scratch.write_text("print('mine')\n")
    mock_settings.allow_dirty = True
    main_log = git(fixture_repo, "log", "--oneline", "main").stdout
    report = run_night(fixture_repo, mock_settings, explicit=True)
    assert (fixture_repo / "scratch.py").read_text() == "print('mine')\n"
    assert "USER WIP" in readme.read_text()
    stat = git(fixture_repo, "log", "--stat", f"main..{report.branch}").stdout
    assert "scratch.py" not in stat
    assert "USER WIP" not in git(fixture_repo, "show", f"{report.branch}:README.md").stdout
    # README.md job is not in the mock brief (widget tests). An upgrade
    # whose path is README.md should void dirty_in_tree — covered when we
    # freeze a README job. Mock brief paths are widget.py / VERSION.
    _ = main_log


def test_dirty_readme_job_is_voided(fixture_repo, mock_settings, monkeypatch):
    from nightshift.llm import mock_upgrades_from_repo
    from nightshift.models import Upgrade

    def fake_upgrades(repo, size=2):
        return [
            Upgrade(1, "touch readme", "true", ["README.md"]),
            Upgrade(2, "ok file", "python -c \"print(1)\"", ["ok.txt"]),
        ][:size]

    monkeypatch.setattr("nightshift.llm.mock_upgrades_from_repo", fake_upgrades)
    (fixture_repo / "README.md").write_text(
        (fixture_repo / "README.md").read_text() + "WIP\n"
    )
    mock_settings.allow_dirty = True
    mock_settings.max_turns = 4
    report = run_night(fixture_repo, mock_settings, explicit=True)
    assert report.brief.upgrades[0].void is True
    assert report.brief.upgrades[0].void_reason == "dirty_in_tree"
