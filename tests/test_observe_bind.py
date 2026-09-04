from __future__ import annotations

from nightshift.observe import _NullScope, start, stop_active


def test_observe_start_survives_busy_port(tmp_path):
    jsonl = tmp_path / "events.jsonl"
    port = 17991
    first = start(open_browser=False, jsonl=str(jsonl), port=port)
    second = start(open_browser=False, jsonl=str(tmp_path / "events2.jsonl"), port=port)
    assert second is not None
    assert hasattr(second, "hold")
    assert not isinstance(second, _NullScope)
    _ = first
    stop_active()


def test_second_start_is_not_null_scope_after_release(tmp_path):
    port = 17995
    first = start(open_browser=False, jsonl=str(tmp_path / "a.jsonl"), port=port)
    second = start(open_browser=False, jsonl=str(tmp_path / "b.jsonl"), port=port)
    try:
        assert not isinstance(second, _NullScope)
        assert hasattr(second, "stop")
    finally:
        stop_active()
        _ = first
