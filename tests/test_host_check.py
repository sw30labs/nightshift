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
    apply_host_truth(brief, [failing, passing_a, passing_b])
    # Critic claims upgrade 1 passed. Host said no.
    critic_claimed = {1, 2, 3}
    for upgrade in brief.upgrades:
        if upgrade.id in critic_claimed and upgrade.id == 1:
            assert upgrade.done is False
    assert brief.upgrades[0].done is False
    assert brief.upgrades[1].done is True
    assert brief.remaining_count == 1
