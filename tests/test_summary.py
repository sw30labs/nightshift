from __future__ import annotations

from datetime import datetime

from nightshift.gitops import current_branch
from nightshift.models import Brief
from nightshift.runner import run_night, write_summary
from nightshift.summary import date_from_branch


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
