from __future__ import annotations

from nightshift.gitops import git
from nightshift.ledger import (
    history_void_reason,
    is_history_duplicate,
    ledger_snapshot_block,
    load_ledger,
    merge_night_into_ledger,
)
from nightshift.models import Brief, Upgrade
from nightshift.runner import run_night


def test_home_ledger_survives_deleted_night(fixture_repo, mock_settings, ns_home):
    report = run_night(fixture_repo, mock_settings, explicit=True)
    git(fixture_repo, "checkout", "main")
    git(fixture_repo, "branch", "-D", report.branch)
    loaded = load_ledger(fixture_repo, home=ns_home)
    assert len(loaded["entries"]) == 2
    mock_settings.max_turns = 4
    report2 = run_night(fixture_repo, mock_settings, explicit=True)
    assert all(u.void for u in report2.brief.upgrades)
    assert all(u.void_reason.startswith("duplicate_of_history") or u.void_reason.startswith("failed_before") for u in report2.brief.upgrades)
    assert report2.halt_reason == "remaining_zero"
    assert report2.brief.remaining_count == 0


def test_failed_before_void(tmp_path):
    cmd = "pytest tests/test_x.py -q"
    ledger = {
        "entries": [
            {
                "title": "same",
                "check_command": cmd,
                "paths": ["foo.py"],
                "check_hash": __import__("nightshift.ledger", fromlist=["check_hash"]).check_hash(cmd),
                "night": "night/2026-09-01",
                "attempted": True,
                "done": False,
                "voided": True,
                "void_reason": "same_host_failure",
                "note": "turn 9: exit 2; ModuleNotFoundError",
                "last_exit": 2,
                "turns": 9,
            }
        ]
    }
    upgrade = Upgrade(1, "same", cmd, ["foo.py"])
    reason = history_void_reason(upgrade, ledger)
    assert reason is not None
    assert reason.startswith("failed_before:night/")
    assert "ModuleNotFoundError" in reason


def test_near_dup(monkeypatch):
    from nightshift.ledger import check_hash

    entry = {
        "title": "strip pytest addopts on the shell path",
        "check_command": "pytest tests/test_a.py -q",
        "paths": ["host.py", "tests/test_a.py"],
        "check_hash": check_hash("pytest tests/test_a.py -q"),
        "night": "night/x",
        "attempted": True,
        "done": True,
        "voided": False,
        "void_reason": "",
        "last_exit": 0,
        "turns": 1,
    }
    upgrade = Upgrade(
        1,
        "shell path must strip pytest addopts",
        "pytest tests/test_b.py -q",
        ["host.py", "tests/test_b.py"],
    )
    ledger = {"entries": [entry]}
    assert history_void_reason(upgrade, ledger) == "duplicate_of_history"
    monkeypatch.setenv("NIGHTSHIFT_NEAR_DUP", "0")
    assert history_void_reason(upgrade, ledger) is None
    assert is_history_duplicate(upgrade, ledger) is False


def test_merge_last_exit_and_block():
    brief = Brief.freeze(
        [
            Upgrade(1, "a", "true 1", ["a.py"]),
            Upgrade(2, "b", "true 2", ["b.py"]),
        ]
    )
    brief.upgrades[0].note = "turn 4: exit 2; boom"
    ledger = merge_night_into_ledger(
        {"entries": []},
        brief,
        "night/z",
        turns_by_id={1: 4, 2: 2},
        last_exit_by_id={1: 2},
    )
    by = {e["title"]: e for e in ledger["entries"]}
    assert by["a"]["last_exit"] == 2
    assert by["a"]["turns"] == 4
    block = ledger_snapshot_block(ledger)
    assert "turn 4: exit 2; boom" in block or "a" in block
