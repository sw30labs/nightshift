from __future__ import annotations

from nightshift.graph import apply_host_truth
from nightshift.host import run_check
from nightshift.models import Brief, Upgrade


def test_host_check_is_truth_not_critic_opinion(fixture_repo):
    brief = Brief.freeze(
        [
            Upgrade(
                1,
                "add",
                f"python -m pytest tests/test_widget.py::test_add -q --rootdir=.",
                ["widget.py"],
            ),
            Upgrade(2, "noop-a", "python -c \"print('a')\"", ["widget.py"]),
            Upgrade(3, "noop-b", "python -c \"print('b')\"", ["widget.py"]),
        ]
    )
    failing = run_check(fixture_repo, brief.upgrades[0], timeout=30)
    passing_a = run_check(fixture_repo, brief.upgrades[1], timeout=30)
    passing_b = run_check(fixture_repo, brief.upgrades[2], timeout=30)
    assert failing.ok is False
    assert passing_a.ok and passing_b.ok
    apply_host_truth(
        brief,
        [failing, passing_a, passing_b],
        job_id=1,
        night_changed={"widget.py"},
    )
    # Critic claims upgrade 1 passed. Host said no.
    assert brief.upgrades[0].done is False
    # Other jobs' green checks do not certify them.
    assert brief.upgrades[1].done is False
    assert brief.upgrades[2].done is False
    assert brief.remaining_count == 3


def test_apply_host_truth_marks_current_job_when_paths_changed():
    brief = Brief.freeze(
        [
            Upgrade(1, "a", "true", ["a.py"]),
            Upgrade(2, "b", "true", ["b.py"]),
        ]
    )
    from nightshift.models import CheckResult

    ok = CheckResult(1, "true", True, 0, "")
    other = CheckResult(2, "true", True, 0, "")
    apply_host_truth(brief, [ok, other], job_id=1, night_changed={"a.py"})
    assert brief.upgrades[0].done is True
    assert brief.upgrades[1].done is False
