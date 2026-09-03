from __future__ import annotations

from nightshift.graph import (
    FAIL_STREAK_LIMIT,
    apply_host_truth,
    bump_fail_streak,
    fail_fingerprint,
    job_paths_changed,
)
from nightshift.models import Brief, CheckResult, Upgrade


def test_fail_fingerprint_collapses_identical_import_errors():
    a = fail_fingerprint(2, "foo\nModuleNotFoundError: No module named 'requests'\nbar")
    b = fail_fingerprint(2, "zzz\nModuleNotFoundError: No module named 'requests'\n")
    assert a == b
    c = fail_fingerprint(2, "ModuleNotFoundError: No module named 'httpx'")
    assert a != c


def test_bump_fail_streak_resets_on_new_fingerprint_or_job():
    s = bump_fail_streak({}, 1, "2:import")
    assert s["count"] == 1
    s = bump_fail_streak(s, 1, "2:import")
    assert s["count"] == 2
    s = bump_fail_streak(s, 1, "2:syntax")
    assert s["count"] == 1
    s = bump_fail_streak(s, 2, "2:syntax")
    assert s["upgrade_id"] == 2
    assert s["count"] == 1


def test_job_paths_changed_requires_overlap():
    assert job_paths_changed({"tests/test_server.py"}, ["tests/test_server.py"]) is True
    assert job_paths_changed({"indio/critic.py"}, ["tests/test_server.py"]) is False
    assert job_paths_changed(set(), ["tests/test_server.py"]) is False
    assert job_paths_changed({"tests/test_server.py"}, []) is False


def test_green_other_job_is_not_done():
    brief = Brief.freeze(
        [
            Upgrade(1, "server", "false", ["tests/test_server.py"]),
            Upgrade(2, "critic", "true", ["indio/critic.py", "tests/test_critic.py"]),
        ]
    )
    apply_host_truth(
        brief,
        [
            CheckResult(1, "false", False, 1, "import requests"),
            CheckResult(2, "true", True, 0, "4 passed"),
        ],
        job_id=1,
        night_changed={"tests/test_server.py"},
    )
    assert brief.upgrades[0].done is False
    assert brief.upgrades[1].done is False


def test_current_job_green_but_paths_unchanged_is_not_done():
    brief = Brief.freeze(
        [Upgrade(1, "critic", "true", ["indio/critic.py"]), Upgrade(2, "other", "true", ["b.py"])]
    )
    apply_host_truth(
        brief,
        [CheckResult(1, "true", True, 0, "4 passed"), CheckResult(2, "true", True, 0, "")],
        job_id=1,
        night_changed=set(),
    )
    assert brief.upgrades[0].done is False


def test_three_identical_fails_voids_and_unlocks_next():
    brief = Brief.freeze(
        [
            Upgrade(1, "server", "false", ["tests/test_server.py"]),
            Upgrade(2, "jobs", "true", ["tests/test_jobs.py"]),
        ]
    )
    fp = fail_fingerprint(2, "ModuleNotFoundError: No module named 'requests'")
    streak: dict = {}
    for _ in range(FAIL_STREAK_LIMIT):
        streak = bump_fail_streak(streak, 1, fp)
    assert streak["count"] == FAIL_STREAK_LIMIT
    brief.void_upgrade(1, "same_host_failure")
    assert brief.upgrades[0].void is True
    assert [u.id for u in brief.remaining()] == [2]
