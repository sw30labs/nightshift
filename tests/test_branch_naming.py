from __future__ import annotations

from datetime import datetime

from nightshift.gitops import checkout_night_branch, list_local_branches, night_branch_name


def test_night_branch_name_appends_hhmm_on_collision():
    now = datetime(2026, 9, 2, 1, 5)
    assert night_branch_name(set(), now) == "night/2026-09-02"
    assert night_branch_name({"night/2026-09-02"}, now) == "night/2026-09-02-0105"
    assert (
        night_branch_name({"night/2026-09-02", "night/2026-09-02-0105"}, now)
        == "night/2026-09-02-010500"
    )


def test_checkout_creates_night_branch_not_main(fixture_repo):
    now = datetime(2026, 9, 2, 1, 7)
    name = checkout_night_branch(fixture_repo, now)
    assert name == "night/2026-09-02"
    name2 = checkout_night_branch(fixture_repo, now)
    assert name2 == "night/2026-09-02-0107"
    branches = list_local_branches(fixture_repo)
    assert "main" in branches
    assert name in branches
    assert name2 in branches
