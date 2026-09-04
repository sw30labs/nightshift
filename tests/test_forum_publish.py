from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from nightshift import cli
from nightshift.config import Settings
from nightshift.forum import ingest_forum, load_forum, mark_merged, publish_error_stub, publish_night
from nightshift.gitops import current_branch, git, list_local_branches
from nightshift.ledger import check_hash, item_id, load_ledger, repo_id
from nightshift.llm import Critic, mock_upgrades_from_repo
from nightshift.models import Brief, SafetyError, Upgrade
from nightshift.runner import (
    LENS_BLOCK_OE,
    NightReport,
    dry_run_brief,
    freeze_lens_hint,
    run_night,
)
from nightshift.status import StatusBoard

AINEKO = "Aineko · portfolio ledger · not a chat."


class _CriticDown:
    """Same shape as tests/test_dry_run.py: freeze fails before any branch exists."""

    mock = False

    def __init__(self, *a, **k) -> None:
        pass

    def propose_brief(self, *a, **k):
        raise RuntimeError("critic down")


def _dated(ns_home: Path, day: int) -> Settings:
    return Settings(
        mock=True,
        observe=False,
        home=ns_home,
        max_turns=12,
        stall_after=12,
        check_timeout=30,
        push=False,
        now_fn=lambda: datetime(2026, 9, day, 1, 0),
    )


def _clone_rows_by_hash(repo: Path) -> dict[str, dict]:
    data = json.loads((repo / ".nightshift" / "ledger.json").read_text(encoding="utf-8"))
    return {e["check_hash"]: e for e in data["entries"]}


# --- path (1): success ---------------------------------------------------------


def test_publish_success_path(fixture_repo, mock_settings, ns_home):
    report = run_night(fixture_repo, mock_settings, explicit=True)
    rid = repo_id(fixture_repo)
    assert (ns_home / "forum.json").is_file()
    forum = load_forum(ns_home)
    assert len(forum["nights"]) == 1
    night = forum["nights"][0]
    assert night["halt_reason"] == "remaining_zero"
    assert night["night"] == report.branch == night["branch"]
    assert night["id"] == f"n-{rid}-" + report.branch.replace("/", "-")
    assert night["repo_id"] == rid and night["repo_name"] == fixture_repo.name
    assert night["repo_path"] == str(fixture_repo) and night["meta"] is False
    assert (night["landed"], night["voided"], night["remaining"]) == (2, 0, 0)
    assert len(night["item_ids"]) == 2
    assert night["mock"] is True
    assert night["merged"] is False and night["merged_by"] == ""
    assert night["base_ref"] == "main" and night["base_sha"] == report.base_sha
    assert night["main_untouched"] is True and night["error"] == ""
    assert night["brief_size"] == 2 and night["lens_hint"] == "de"
    assert night["started_at"] and night["ended_at"] >= night["started_at"]
    assert (report.started_at, report.ended_at) == (night["started_at"], night["ended_at"])
    assert report.mock is True and report.lens_hint == "de"
    assert (report.landed, report.voided) == (2, 0) and report.error == ""
    # items agree with the clone ledger rows just written
    assert len(forum["items"]) == 2
    rows = _clone_rows_by_hash(fixture_repo)
    for item in forum["items"]:
        assert item["id"].startswith(f"i-{rid}-") and item["id"] in night["item_ids"]
        assert item["attempted"] is True and item["done"] is True and item["voided"] is False
        assert item["paths"] == ["widget.py"] and item["night"] == report.branch
        assert item["lens"] == "de" and item["repo_name"] == fixture_repo.name
        row = rows[item["check_hash"]]
        assert (row["done"], row["attempted"]) == (item["done"], item["attempted"])
        assert item["title"] == row["title"] and item["check_command"] == row["check_command"]
    assert forum["reuse_events"] == [] and forum["errors"] == []
    md = (ns_home / "forum.md").read_text(encoding="utf-8")
    assert md.count(AINEKO) == 1
    assert "## Tonight's bag" not in md
    assert "[done] Make test_add pass" in md and "[done] Make test_greet pass" in md
    assert f"git checkout main && git merge --no-ff {report.branch}" in md
    # re-publishing the same report is idempotent
    again = publish_night(
        home=ns_home, report=report, ledger=load_ledger(fixture_repo, home=ns_home)
    )
    assert len(again["nights"]) == 1 and len(again["items"]) == 2
    # an existing merged=True is never clobbered by a publish
    mark_merged(ns_home, fixture_repo, night=report.branch)
    again = publish_night(
        home=ns_home, report=report, ledger=load_ledger(fixture_repo, home=ns_home)
    )
    assert again["nights"][0]["merged"] is True
    assert again["nights"][0]["merged_by"] == "operator"
    assert len(again["nights"]) == 1


# --- path (2): Ralph crash -----------------------------------------------------


def test_publish_crash_path(fixture_repo, mock_settings, ns_home, monkeypatch):
    def boom(self, state):
        raise RuntimeError("writer exploded")

    monkeypatch.setattr("nightshift.graph.LoopNodes.writer", boom)
    with pytest.raises(RuntimeError, match="writer exploded") as excinfo:
        run_night(fixture_repo, mock_settings, explicit=True)
    # N3: the crash is already a night row, so a bag must not stub it again
    assert getattr(excinfo.value, "nightshift_forum_published", False) is True
    branch = current_branch(fixture_repo)
    assert branch.startswith("night/")
    forum = load_forum(ns_home)
    assert len(forum["nights"]) == 1
    night = forum["nights"][0]
    assert night["halt_reason"] == "error"
    assert "writer exploded" in night["error"]
    assert night["night"] == branch == night["branch"]
    assert (night["landed"], night["voided"], night["remaining"]) == (0, 0, 2)
    assert night["brief_size"] == 2 and len(night["item_ids"]) == 2
    assert night["mock"] is True and night["merged"] is False
    assert night["started_at"] and night["ended_at"]
    assert len(forum["items"]) == 2
    for item in forum["items"]:
        assert item["attempted"] is False and item["done"] is False
        assert item["night"] == branch and item["paths"] == ["widget.py"]
        assert item["id"] in night["item_ids"]
    assert forum["errors"] == []  # path (2) is a night row, not a bag error
    md = (ns_home / "forum.md").read_text(encoding="utf-8")
    assert "error: writer exploded" in md
    assert "[open] Make test_add pass" in md


def test_publish_night_without_brief_has_no_items(fixture_repo, ns_home):
    # NightReport with only the required fields: brief / summary_path optional
    report = NightReport(
        repo=fixture_repo,
        branch="night/2026-09-03",
        main_ref="main",
        main_sha="",
        remaining_count=0,
        halt_reason="error",
        error="x" * 900,
    )
    assert report.brief is None and report.summary_path is None and report.refused == []
    forum = publish_night(home=ns_home, report=report, ledger={"entries": []})
    assert forum["items"] == [] and len(forum["nights"]) == 1
    night = forum["nights"][0]
    assert night["item_ids"] == [] and night["brief_size"] == 0
    assert (night["landed"], night["voided"], night["remaining"]) == (0, 0, 0)
    assert len(night["error"]) == 500
    assert night["merged"] is False


def test_publish_night_filters_blocked_paths_and_projects_ledger_rows(fixture_repo, ns_home):
    rid = repo_id(fixture_repo)
    cmd_a, cmd_b = "pytest tests/test_a.py -q", "pytest tests/test_b.py -q"
    brief = Brief.freeze(
        [
            Upgrade(1, "touch env", cmd_a, ["widget.py", ".env", "./widget.py"]),
            Upgrade(2, "older keeper", cmd_b, ["host.py"]),
        ],
        branch="night/2026-09-04",
    )
    brief.upgrades[1].void = True
    brief.upgrades[1].void_reason = "duplicate_of_history"
    ledger = {
        "entries": [
            {
                # merge_night_into_ledger refused to clobber this landed row:
                # it projects as the older night, not as tonight's void
                "title": "older keeper",
                "check_command": cmd_b,
                "paths": ["host.py"],
                "check_hash": check_hash(cmd_b),
                "night": "night/2026-09-01",
                "attempted": True,
                "done": True,
                "voided": False,
                "void_reason": "",
                "last_exit": 0,
                "turns": 2,
                "note": "landed turn 2",
            }
        ]
    }
    report = NightReport(
        repo=fixture_repo,
        branch="night/2026-09-04",
        main_ref="main",
        main_sha="",
        remaining_count=1,
        halt_reason="max_turns",
        brief=brief,
        lens_hint="oe",
    )
    forum = publish_night(home=ns_home, report=report, ledger=ledger)
    by_hash = {i["check_hash"]: i for i in forum["items"]}
    fresh = by_hash[check_hash(cmd_a)]
    assert fresh["paths"] == ["widget.py"]
    assert fresh["id"] == item_id(rid, check_hash(cmd_a), ["widget.py"])
    assert fresh["attempted"] is False and fresh["done"] is False
    assert fresh["night"] == "night/2026-09-04" and fresh["lens"] == "oe"
    kept = by_hash[check_hash(cmd_b)]
    assert kept["done"] is True and kept["voided"] is False
    assert kept["night"] == "night/2026-09-01" and kept["note"] == "landed turn 2"
    assert kept["lens"] == ""
    night = forum["nights"][0]
    assert (night["landed"], night["voided"], night["remaining"]) == (0, 1, 1)
    assert set(night["item_ids"]) == {fresh["id"], kept["id"]}
    assert ".env" not in (ns_home / "forum.json").read_text(encoding="utf-8")


# --- path (3): freeze fails on base --------------------------------------------


def test_publish_freeze_fail_stub(fixture_repo, mock_settings, ns_home, monkeypatch):
    monkeypatch.setattr("nightshift.runner.Critic", _CriticDown)
    with pytest.raises(RuntimeError, match="critic down") as excinfo:
        run_night(fixture_repo, mock_settings, explicit=True)
    assert getattr(excinfo.value, "nightshift_forum_published", False) is True
    assert current_branch(fixture_repo) == "main"
    assert not any(b.startswith("night/") for b in list_local_branches(fixture_repo))
    forum = load_forum(ns_home)
    assert len(forum["errors"]) == 1
    err = forum["errors"][0]
    assert err["repo_id"] == repo_id(fixture_repo) and "critic down" in err["error"]
    assert err["repo_name"] == fixture_repo.name and err["repo_path"] == str(fixture_repo)
    assert err["bag_id"] == "" and err["at"]
    assert len(forum["nights"]) == 1
    stub = forum["nights"][0]
    assert stub["night"].startswith("error/") and stub["night"] == "error/" + stub["started_at"]
    assert stub["branch"] == "" and stub["item_ids"] == [] and stub["halt_reason"] == "error"
    assert "critic down" in stub["error"]
    assert stub["mock"] is True and stub["meta"] is False and stub["merged"] is False
    assert stub["brief_size"] == 0 and stub["base_ref"] == ""
    assert forum["items"] == [] and forum["reuse_events"] == []
    md = (ns_home / "forum.md").read_text(encoding="utf-8")
    assert "## Errors\n- " in md and "critic down" in md
    assert "## Land\n- (none)" in md
    # a later freeze failure is its own stub (N1), the same clock is one stub
    mock_settings.now_fn = lambda: datetime(2026, 9, 3, 2, 0, 0)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="critic down"):
            run_night(fixture_repo, mock_settings, explicit=True)
    forum = load_forum(ns_home)
    assert len(forum["nights"]) == 2 and len(forum["errors"]) == 2
    assert "error/2026-09-03T02:00:00" in {n["night"] for n in forum["nights"]}


def test_publish_error_stub_direct(fixture_repo, ns_home):
    forum = publish_error_stub(
        home=ns_home,
        repo=fixture_repo,
        error="working tree has 3 uncommitted changes",
        mock=False,
        started_at="2026-09-03T05:00:00",
        bag_id="b-20260903-1",
    )
    assert forum["nights"][0]["night"] == "error/2026-09-03T05:00:00"
    assert forum["nights"][0]["mock"] is False
    assert forum["errors"][0]["bag_id"] == "b-20260903-1"
    forum = publish_error_stub(
        home=ns_home,
        repo=fixture_repo,
        error="working tree has 3 uncommitted changes",
        started_at="2026-09-03T05:00:00",
        bag_id="b-20260903-1",
    )
    assert len(forum["nights"]) == 1 and len(forum["errors"]) == 1


# --- toggles: NIGHTSHIFT_FORUM=0 and dry-run -----------------------------------


def test_forum_disabled_publishes_nothing(fixture_repo, mock_settings, ns_home, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_FORUM", "0")
    report = run_night(fixture_repo, mock_settings, explicit=True)
    assert report.halt_reason == "remaining_zero"
    assert not (ns_home / "forum.json").exists()
    assert not (ns_home / "forum.md").exists()
    monkeypatch.setattr("nightshift.runner.Critic", _CriticDown)
    with pytest.raises(RuntimeError, match="critic down") as excinfo:
        run_night(fixture_repo, mock_settings, explicit=True)
    assert not hasattr(excinfo.value, "nightshift_forum_published")
    assert not (ns_home / "forum.json").exists()


def test_dry_run_brief_does_not_publish(fixture_repo, mock_settings, ns_home):
    brief = dry_run_brief(fixture_repo, mock_settings, explicit=True)
    assert len(brief.upgrades) == 2
    assert not (ns_home / "forum.json").exists()
    assert not (ns_home / "forum.md").exists()


# --- guards: a publish failure never fails a night; a failed tail never drops one --


def _record_runner_logs(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr("nightshift.runner.log", lambda text, *a, **k: seen.append(str(text)))
    return seen


def _raise_disk_full(**kwargs):
    raise RuntimeError("disk full")


def test_publish_failure_never_fails_the_night(fixture_repo, mock_settings, ns_home, monkeypatch):
    # Key Decision 2 / section 9: publish_night raising on path (1) is one log line.
    monkeypatch.setattr("nightshift.forum.publish_night", _raise_disk_full)
    logs = _record_runner_logs(monkeypatch)
    report = run_night(fixture_repo, mock_settings, explicit=True)
    assert report.halt_reason == "remaining_zero" and (report.landed, report.voided) == (2, 0)
    assert report.error == ""
    rows = _clone_rows_by_hash(fixture_repo)
    assert len(rows) == 2 and all(r["done"] and r["attempted"] for r in rows.values())
    assert "nightshift: update ledger" in git(fixture_repo, "log", "--format=%s", "-3").stdout
    board = StatusBoard(ns_home).read()
    assert board.state == "done" and board.runner_pid is None
    assert not (ns_home / "forum.json").exists() and not (ns_home / "forum.md").exists()
    assert [ln for ln in logs if ln.startswith("forum publish failed")] == [
        "forum publish failed: disk full"
    ]


def test_error_stub_failure_leaves_exception_unmarked(
    fixture_repo, mock_settings, ns_home, monkeypatch
):
    # N3: an exception the stub could not record stays unmarked so run_bag stubs it.
    monkeypatch.setattr("nightshift.runner.Critic", _CriticDown)
    monkeypatch.setattr("nightshift.forum.publish_error_stub", _raise_disk_full)
    logs = _record_runner_logs(monkeypatch)
    with pytest.raises(RuntimeError, match="critic down") as excinfo:
        run_night(fixture_repo, mock_settings, explicit=True)
    assert not hasattr(excinfo.value, "nightshift_forum_published")
    assert current_branch(fixture_repo) == "main"
    assert not any(b.startswith("night/") for b in list_local_branches(fixture_repo))
    assert StatusBoard(ns_home).read().state == "error"
    assert not (ns_home / "forum.json").exists() and not (ns_home / "forum.md").exists()
    assert [ln for ln in logs if ln.startswith("forum error stub failed")] == [
        "forum error stub failed: disk full"
    ]


def test_push_failure_after_ledger_still_publishes(fixture_repo, mock_settings, ns_home, monkeypatch):
    # Section 2: every completed run_night publishes, path (1) right after
    # _commit_ledger. A push (or board) failure in the tail is outside the Ralph
    # try, so it must not drop the row; it is recorded on the night instead.
    calls: list[str] = []
    real_publish = publish_night

    def counting_publish(**kwargs):
        calls.append(kwargs["report"].branch)
        return real_publish(**kwargs)

    def bad_push(repo, branch):
        raise SafetyError("git push failed: remote unreachable")

    monkeypatch.setattr("nightshift.forum.publish_night", counting_publish)
    monkeypatch.setattr("nightshift.runner.push_branch", bad_push)
    with pytest.raises(SafetyError, match="remote unreachable") as excinfo:
        run_night(fixture_repo, replace(mock_settings, push=True), explicit=True)
    assert getattr(excinfo.value, "nightshift_forum_published", False) is True  # N3
    branch = current_branch(fixture_repo)
    assert branch.startswith("night/") and calls == [branch]
    rows = _clone_rows_by_hash(fixture_repo)
    assert len(rows) == 2 and all(r["done"] for r in rows.values())
    forum = load_forum(ns_home)
    assert len(forum["nights"]) == 1
    night = forum["nights"][0]
    assert night["night"] == branch == night["branch"]
    assert night["halt_reason"] == "remaining_zero"
    assert (night["landed"], night["voided"], night["remaining"]) == (2, 0, 0)
    assert len(night["item_ids"]) == 2 and night["brief_size"] == 2
    assert "remote unreachable" in night["error"] and night["merged"] is False
    assert night["started_at"] and night["ended_at"] >= night["started_at"]
    assert len(forum["items"]) == 2
    assert all(i["done"] is True and i["attempted"] is True for i in forum["items"])
    assert forum["errors"] == []  # path (1) is a night row, not a bag error
    md = (ns_home / "forum.md").read_text(encoding="utf-8")
    assert "error: git push failed: remote unreachable" in md
    assert "[done] Make test_add pass" in md and "[done] Make test_greet pass" in md
    assert f"git checkout main && git merge --no-ff {branch}" in md
    # the success path with a working push still publishes exactly once
    monkeypatch.setattr("nightshift.runner.push_branch", lambda repo, branch: "")
    git(fixture_repo, "checkout", "main")
    calls.clear()
    report = run_night(fixture_repo, replace(mock_settings, push=True), explicit=True)
    assert calls == [report.branch] and report.error == ""
    forum = load_forum(ns_home)
    assert {n["night"]: n["error"] for n in forum["nights"]} == {
        branch: "git push failed: remote unreachable",
        report.branch: "",
    }


@pytest.mark.parametrize(
    "broken, message",
    [("write_summary", "summary write failed"), ("_commit_ledger", "ledger commit failed")],
)
def test_tail_failure_after_ralph_still_publishes(
    fixture_repo, mock_settings, ns_home, monkeypatch, broken, message
):
    # Section 2: every completed run_night publishes. A summary or ledger
    # failure after the Ralph loop is neither path (2) nor path (1) tail, yet
    # the night has a branch with turn commits: it must land as a night row
    # (halt_reason error, not an error/<ts> stub), leave the board off
    # "running", and carry the N3 marker so a bag does not stub it again.
    def bad(*a, **k):
        raise OSError(message)

    monkeypatch.setattr(f"nightshift.runner.{broken}", bad)
    with pytest.raises(OSError, match=message) as excinfo:
        run_night(fixture_repo, mock_settings, explicit=True)
    assert getattr(excinfo.value, "nightshift_forum_published", False) is True
    branch = current_branch(fixture_repo)
    assert branch.startswith("night/")
    board = StatusBoard(ns_home).read()
    assert board.state == "error" and board.runner_pid is None
    assert board.halt_reason == "remaining_zero" and message in board.error
    forum = load_forum(ns_home)
    assert len(forum["nights"]) == 1 and forum["errors"] == []
    night = forum["nights"][0]
    assert night["night"] == branch == night["branch"]
    assert not night["night"].startswith("error/")
    assert night["halt_reason"] == "error" and message in night["error"]
    assert (night["landed"], night["voided"], night["remaining"]) == (2, 0, 0)
    assert night["brief_size"] == 2 and len(night["item_ids"]) == 2
    assert night["started_at"] and night["ended_at"] >= night["started_at"]
    assert night["mock"] is True and night["merged"] is False
    assert len(forum["items"]) == 2
    assert all(i["night"] == branch and i["paths"] == ["widget.py"] for i in forum["items"])
    summary = fixture_repo / ".nightshift" / "summary.md"
    ledger = fixture_repo / ".nightshift" / "ledger.json"
    if broken == "write_summary":
        # nothing after the summary ran: no ledger, items project from the brief
        assert not summary.exists() and not ledger.exists()
        assert all(i["attempted"] is False for i in forum["items"])
    else:
        assert summary.is_file() and not ledger.exists()
        assert all(i["attempted"] is False for i in forum["items"])
    md = (ns_home / "forum.md").read_text(encoding="utf-8")
    assert f"error: {message}" in md


# --- second night: void from history, done rows kept -----------------------------


def test_second_night_voids_history_and_keeps_done_items(fixture_repo, ns_home):
    r1 = run_night(fixture_repo, _dated(ns_home, 3), explicit=True)
    r2 = run_night(fixture_repo, _dated(ns_home, 4), explicit=True)
    assert r2.branch != r1.branch
    assert all(u.void and u.void_reason == "duplicate_of_history" for u in r2.brief.upgrades)
    assert r2.halt_reason == "remaining_zero"
    assert (r2.landed, r2.voided) == (0, 2) and (r1.landed, r1.voided) == (2, 0)
    forum = load_forum(ns_home)
    assert len(forum["items"]) == 2
    for item in forum["items"]:
        assert item["done"] is True and item["voided"] is False
        assert item["night"] == r1.branch
        # night 2 re-projects the kept done row without a lens; night 1's stays
        assert item["lens"] == "de"
    by_night = {n["night"]: n for n in forum["nights"]}
    assert set(by_night) == {r1.branch, r2.branch}
    second = by_night[r2.branch]
    assert (second["landed"], second["voided"], second["remaining"]) == (0, 2, 0)
    assert by_night[r1.branch]["landed"] == 2
    assert sorted(second["item_ids"]) == sorted(by_night[r1.branch]["item_ids"])
    md = (ns_home / "forum.md").read_text(encoding="utf-8")
    assert "[done] Make test_add pass" in md and "[void" not in md
    assert f"landed 0 / void 2 / open 0" in md
    # a morning ingest after both nights leaves the published items alone
    forum = ingest_forum(ns_home, [fixture_repo])
    assert len(forum["items"]) == 2
    assert all(i["lens"] == "de" and i["night"] == r1.branch for i in forum["items"])


# --- lens hint at freeze ----------------------------------------------------------


def test_lens_hint_de_then_oe_from_home_ledger(fixture_repo, ns_home, monkeypatch):
    seen: list[str] = []

    class Spy(Critic):
        def propose_brief(self, snapshot, size=2):
            seen.append(snapshot)
            return mock_upgrades_from_repo(self.repo, size=size)

    monkeypatch.setattr("nightshift.runner.Critic", Spy)
    assert LENS_BLOCK_OE == (
        "## Freeze lens\n"
        "This clone has operational evidence (ledger / last host checks).\n"
        "Prefer at least one upgrade that hardens a previously attempted check "
        "if that is still checkable.\n"
        "JOBS N is the total bag. Do not emit DE and OE as separate tracks.\n"
    )
    assert freeze_lens_hint(fixture_repo, ns_home) == "de"
    r1 = run_night(fixture_repo, _dated(ns_home, 3), explicit=True)
    assert r1.lens_hint == "de"
    assert "## Freeze lens" not in seen[0]
    assert "## Prior night ledger" not in seen[0]
    # drop the night branch: only the home shard remembers the evidence
    git(fixture_repo, "checkout", "main")
    git(fixture_repo, "branch", "-D", r1.branch)
    assert not (fixture_repo / ".nightshift" / "ledger.json").exists()
    assert freeze_lens_hint(fixture_repo, ns_home) == "oe"
    r2 = run_night(fixture_repo, _dated(ns_home, 4), explicit=True)
    assert r2.lens_hint == "oe"
    assert seen[1].startswith(LENS_BLOCK_OE + "\n# repo ")
    assert seen[1].count("## Freeze lens") == 1
    assert "## Prior night ledger" in seen[1] and "Make test_add pass" in seen[1]
    assert "## Portfolio forum" not in seen[1]  # PR 3
    forum = load_forum(ns_home)
    assert {n["night"]: n["lens_hint"] for n in forum["nights"]} == {
        r1.branch: "de",
        r2.branch: "oe",
    }


# --- CLI: nightshift forum / --json / ingest ---------------------------------------


def test_cli_forum_prints_and_ingests(fixture_repo, mock_settings, ns_home, capsys, tmp_path):
    assert cli.main(["forum"]) == 1
    assert "no forum yet" in capsys.readouterr().err
    assert cli.main(["forum", "--json"]) == 1
    capsys.readouterr()
    run_night(fixture_repo, mock_settings, explicit=True)
    assert cli.main(["forum"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Nightshift forum\n" + AINEKO + "\n")
    assert "[done] Make test_add pass" in out
    assert cli.main(["forum", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema"] == 1 and len(data["nights"]) == 1 and len(data["items"]) == 2
    assert cli.main(["forum", "ingest", "--roots", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "repos 1  nights 1  items 2  orphans 0"
    forum = load_forum(ns_home)
    assert len(forum["nights"]) == 1 and forum["nights"][0]["lens_hint"] == "de"
    # ingest is additive on the items the live publish wrote: it has no lens to give
    assert [i["lens"] for i in forum["items"]] == ["de", "de"]
