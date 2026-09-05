from __future__ import annotations

import json
from datetime import datetime

from nightshift.gitops import current_branch, git, rev_parse
from nightshift.models import Brief, Upgrade
from nightshift.runner import run_night, write_summary
from nightshift.summary import date_from_branch, night_view


def test_summary_morning_shape(fixture_repo, mock_settings):
    report = run_night(fixture_repo, mock_settings, explicit=True)
    text = report.summary_path.read_text()
    assert "2 of 2 landed" in text
    assert "## Land it" in text
    assert "merge --no-ff night/" in text
    fences = text.split("## What changed", 1)[1].split("## Land it", 1)[0].split("```")
    # second fence is git diff --stat
    stat_fence = fences[3] if len(fences) > 3 else fences[-2] if len(fences) > 1 else ""
    assert ".nightshift" not in stat_fence
    assert "## What changed" in text
    assert "## What the critic refused" in text
    keepers = [u for u in report.brief.upgrades if u.done]
    assert keepers


def test_summary_title_uses_branch_date(fixture_repo, mock_settings):
    mock_settings.now_fn = lambda: datetime(2026, 9, 3, 23, 30, 0)
    report = run_night(fixture_repo, mock_settings, explicit=True)
    assert report.branch.startswith("night/2026-09-03")
    text = report.summary_path.read_text()
    assert text.splitlines()[0] == "# Nightshift — 2026-09-03"
    assert date_from_branch("night/2026-09-03-2330", "x") == "2026-09-03"


def test_error_still_writes_summary(fixture_repo, mock_settings, monkeypatch):
    class Boom:
        mock = False
        n = 0

        def apply_job(self, *a, **k):
            self.n += 1
            if self.n >= 2:
                raise RuntimeError("writer exploded")
            from nightshift.llm import MockChatClient, Writer

            return Writer(MockChatClient("writer", fixture_repo), fixture_repo).apply_job(*a, **k)

    monkeypatch.setattr("nightshift.runner.Writer", lambda *a, **k: Boom())
    try:
        run_night(fixture_repo, mock_settings, explicit=True)
        assert False, "expected crash"
    except RuntimeError:
        pass
    assert current_branch(fixture_repo).startswith("night/")
    summary = (fixture_repo / ".nightshift" / "summary.md").read_text()
    assert "error" in summary.lower() or "crashed" in summary.lower()
    assert (fixture_repo / ".nightshift" / "ledger.json").is_file()


def test_max_turns_emits_remaining(fixture_repo, mock_settings, monkeypatch):
    class Noop:
        mock = False

        def apply_job(self, *a, **k):
            from nightshift.models import WriterResult

            return WriterResult(written=[], message="noop", refused=[])

    monkeypatch.setattr("nightshift.runner.Writer", lambda *a, **k: Noop())
    mock_settings.max_turns = 2
    mock_settings.stall_after = 20
    report = run_night(fixture_repo, mock_settings, explicit=True)
    text = report.summary_path.read_text()
    assert "## Remaining" in text
    assert report.remaining_count > 0
    _ = Brief
    _ = write_summary


def test_cherry_pick_keepers_are_unique_and_apply_in_dependency_order(fixture_repo):
    base_sha = rev_parse(fixture_repo, "HEAD")
    branch = "night/2026-09-04"
    git(fixture_repo, "checkout", "-b", branch)

    # The first commit belongs to two completed jobs; the second depends on it.
    (fixture_repo / "widget.py").write_text("value = 1\n")
    (fixture_repo / "README.md").write_text("Shared first change\n")
    git(fixture_repo, "add", "widget.py", "README.md")
    git(fixture_repo, "commit", "-m", "shared first change")
    first = git(fixture_repo, "rev-parse", "--short", "HEAD").stdout.strip()
    (fixture_repo / "widget.py").write_text("value = 2\n")
    git(fixture_repo, "add", "widget.py")
    git(fixture_repo, "commit", "-m", "dependent second change")
    second = git(fixture_repo, "rev-parse", "--short", "HEAD").stdout.strip()

    # Work for an unfinished job must stay out of the keeper command.
    (fixture_repo / "VERSION").write_text("unfinished\n")
    git(fixture_repo, "add", "VERSION")
    git(fixture_repo, "commit", "-m", "unfinished version change")
    unfinished = git(fixture_repo, "rev-parse", "--short", "HEAD").stdout.strip()
    brief = Brief.freeze([
        Upgrade(1, "implementation", "python -m pytest", ["widget.py"]),
        Upgrade(2, "documentation", "test -s README.md", ["README.md"]),
        Upgrade(3, "version", "test -s VERSION", ["VERSION"]),
    ], repo=str(fixture_repo), branch=branch, base_ref="main", base_sha=base_sha)
    brief.mark_done([1, 2])
    meta = fixture_repo / ".nightshift"
    meta.mkdir()
    (meta / "brief.json").write_text(json.dumps(brief.to_dict()))

    view = night_view(fixture_repo)
    assert view["land"]["cherry_pick"] == f"git cherry-pick {first} {second}"
    assert {row["sha"]: row["keeper"] for row in view["commits"]} == {
        first: True, second: True, unfinished: False,
    }
    git(fixture_repo, "checkout", "main")
    git(fixture_repo, *view["land"]["cherry_pick"].split()[1:])
    assert (fixture_repo / "widget.py").read_text() == "value = 2\n"
    assert (fixture_repo / "README.md").read_text() == "Shared first change\n"
    assert not (fixture_repo / "VERSION").exists()
