from __future__ import annotations

import json

from nightshift.graph import pick_job
from nightshift.host import count_failed
from nightshift.llm import Critic, Writer
from nightshift.models import Brief, Upgrade
from nightshift.observe import FallbackRalphLoop
from nightshift.runner import NightReport, _open_work


def _brief():
    return Brief.freeze(
        [
            Upgrade(1, "a", "true", ["a.py"]),
            Upgrade(2, "b", "true", ["b.py"]),
        ]
    )


def test_pick_job_rotates_after_budget():
    brief = _brief()
    turns: dict[str, int] = {}
    seen = []
    for _ in range(10):
        u = pick_job(brief, turns, 4)
        seen.append(u.id)
        turns[str(u.id)] = turns.get(str(u.id), 0) + 1
    assert seen[:4] == [1, 1, 1, 1]
    assert seen[4:8] == [2, 2, 2, 2]
    assert seen[8:10] == [1, 1]
    brief.void_upgrade(1, "same_host_failure")
    for _ in range(5):
        u = pick_job(brief, turns, 4)
        assert u.id == 2
        turns[str(u.id)] = turns.get(str(u.id), 0) + 1


def test_count_failed_and_open_work():
    assert count_failed("2 failed, 3 passed") == 2
    assert count_failed("ERROR collecting tests/test_x.py") == 0
    brief = _brief()
    brief.void_upgrade(2, "skip")
    state_hi = {
        "remaining_count": 1,
        "brief": brief.to_dict(),
        "check_results": [
            {"upgrade_id": 1, "ok": False, "exit_code": 1, "output": "5 failed, 0 passed"}
        ],
    }
    state_lo = {
        "remaining_count": 1,
        "brief": brief.to_dict(),
        "check_results": [
            {"upgrade_id": 1, "ok": False, "exit_code": 1, "output": "1 failed, 4 passed"}
        ],
    }
    assert _open_work(state_lo) < _open_work(state_hi)
    empty = {
        "remaining_count": 1,
        "brief": brief.to_dict(),
        "check_results": [
            {"upgrade_id": 1, "ok": False, "exit_code": 1, "output": "5 failed, 0 passed"}
        ],
    }
    assert _open_work(empty) == _open_work(state_hi)


def test_stalled_halt_reason(fixture_repo, mock_settings, monkeypatch):
    class StallLoop(FallbackRalphLoop):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.reason = "stalled"
            self.max_iters = 2

        def __iter__(self):
            yield from super().__iter__()

    monkeypatch.setattr("nightshift.runner.ralph_loop", lambda *a, **k: StallLoop("x", max_iters=2))
    from nightshift.runner import run_night

    mock_settings.max_turns = 2
    mock_settings.stall_after = 1
    # StallLoop sets reason stalled; with max_iters=2 the mock may still finish.
    # Force the for/else path by making remaining never 0.
    class DeadWriter:
        mock = False

        def apply_job(self, *a, **k):
            from nightshift.models import WriterResult

            return WriterResult(written=[], message="noop", refused=[])

    monkeypatch.setattr("nightshift.runner.Writer", lambda *a, **k: DeadWriter())
    report = run_night(fixture_repo, mock_settings, explicit=True)
    assert isinstance(report, NightReport)
    assert report.halt_reason in {"stalled", "max_turns"}


def test_job_line_and_writer_lock_to_id(fixture_repo):
    brief = Brief.freeze(
        [
            Upgrade(1, "a", "true", ["README.md"]),
            Upgrade(2, "b", "true", ["widget.py"]),
        ]
    )

    class C:
        mock = False

        def chat(self, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            assert payload["upgrade"]["id"] == 2
            return json.dumps({"upgrade_id": 1, "job": "do two"})

    uid, job = Critic(C(), fixture_repo).job_line(brief, upgrade_id=2)
    assert uid == 2
    assert job == "do two"

    class W:
        mock = False

        def chat(self, messages, **kwargs):
            return json.dumps(
                {
                    "files": [
                        {"path": "README.md", "content": "hack\n"},
                        {"path": "widget.py", "content": "def add(a, b):\n    return a + b\n"},
                    ],
                    "message": "ok",
                }
            )

    result = Writer(W(), fixture_repo).apply_job("do two", brief, "", job_upgrade_id=2)
    assert "widget.py" in result.written
    assert "README.md" not in result.written
    assert any("outside job paths" in n for n in result.refused)
