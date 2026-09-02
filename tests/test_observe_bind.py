from __future__ import annotations

from nightshift.observe import start


def test_observe_start_survives_busy_port(tmp_path):
    jsonl = tmp_path / "events.jsonl"
    port = 17991
    first = start(open_browser=False, jsonl=str(jsonl), port=port)
    second = start(open_browser=False, jsonl=str(tmp_path / "events2.jsonl"), port=port)
    assert second is not None
    assert hasattr(second, "hold")
    second.hold()
    _ = first
