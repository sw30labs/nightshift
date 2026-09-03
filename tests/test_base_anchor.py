from __future__ import annotations

from datetime import datetime

from nightshift.gitops import checkout_night_branch, current_branch, git, rev_parse
from nightshift.graph import night_changed_rels
from nightshift.runner import run_night


def test_night_changed_against_base_not_main(fixture_repo):
    git(fixture_repo, "checkout", "-b", "feat/version")
    (fixture_repo / "VERSION").write_text("0.0.1\n")
    git(fixture_repo, "add", "--", "VERSION")
    git(
        fixture_repo,
        "commit",
        "-m",
        "add VERSION",
        extra_env={
            "GIT_AUTHOR_NAME": "Nightshift Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@localhost",
            "GIT_COMMITTER_NAME": "Nightshift Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@localhost",
        },
    )
    base_sha = rev_parse(fixture_repo, "HEAD")
    main_sha = rev_parse(fixture_repo, "main")
    assert night_changed_rels(fixture_repo, base_sha) == set() or all(
        p.startswith(".nightshift/") for p in night_changed_rels(fixture_repo, base_sha)
    )
    assert "VERSION" in night_changed_rels(fixture_repo, main_sha)


def test_mock_e2e_on_feature_branch(fixture_repo, mock_settings):
    git(fixture_repo, "checkout", "-b", "feat/version")
    (fixture_repo / "VERSION").write_text("0.0.1\n")
    git(fixture_repo, "add", "--", "VERSION")
    git(
        fixture_repo,
        "commit",
        "-m",
        "add VERSION",
        extra_env={
            "GIT_AUTHOR_NAME": "Nightshift Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@localhost",
            "GIT_COMMITTER_NAME": "Nightshift Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@localhost",
        },
    )
    main_sha = rev_parse(fixture_repo, "main")
    report = run_night(fixture_repo, mock_settings, explicit=True)
    assert report.base_ref == "feat/version"
    summary = report.summary_path.read_text()
    assert "**Base:** feat/version" in summary
    assert "add VERSION" not in summary.split("## What changed", 1)[1].split("## Land it", 1)[0]
    assert current_branch(fixture_repo).startswith("night/")
    assert rev_parse(fixture_repo, "main") == main_sha
    assert (fixture_repo / ".nightshift" / "brief.json").read_text().find("night/") != -1


def test_cmd_run_exit_3_when_main_moved(fixture_repo, mock_settings, monkeypatch, ns_home):
    from nightshift import cli
    from nightshift.runner import NightReport
    from nightshift.models import Brief, Upgrade

    brief = Brief.freeze(
        [Upgrade(1, "a", "true", ["a.py"]), Upgrade(2, "b", "true", ["b.py"])]
    )

    def fake_run(repo, settings, explicit=True):
        git(repo, "checkout", "main")
        (repo / "moved.txt").write_text("x\n")
        git(repo, "add", "--", "moved.txt")
        git(
            repo,
            "commit",
            "-m",
            "move main",
            extra_env={
                "GIT_AUTHOR_NAME": "Nightshift Fixture",
                "GIT_AUTHOR_EMAIL": "fixture@localhost",
                "GIT_COMMITTER_NAME": "Nightshift Fixture",
                "GIT_COMMITTER_EMAIL": "fixture@localhost",
            },
        )
        return NightReport(
            repo=repo,
            branch="night/2026-09-03",
            main_ref="main",
            main_sha="deadbeef",
            remaining_count=0,
            halt_reason="remaining_zero",
            brief=brief,
            summary_path=repo / ".nightshift" / "summary.md",
            refused=[],
            base_ref="main",
            base_sha="deadbeef",
            main_untouched=False,
        )

    monkeypatch.setattr("nightshift.cli.run_night", fake_run)
    (fixture_repo / ".nightshift").mkdir(exist_ok=True)
    (fixture_repo / ".nightshift" / "summary.md").write_text("# x\n")
    code = cli.main(["run", str(fixture_repo), "--mock", "--no-observe"])
    assert code == 3


def test_checkout_named_branch_still_works(fixture_repo):
    name = checkout_night_branch(fixture_repo, datetime(2026, 9, 2, 1, 7), name="night/custom")
    assert name == "night/custom"
    assert current_branch(fixture_repo) == "night/custom"
