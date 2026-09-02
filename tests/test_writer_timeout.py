from __future__ import annotations

from nightshift.llm import Writer
from nightshift.models import Brief, Upgrade


def _brief() -> Brief:
    return Brief.freeze(
        [
            Upgrade(id=1, title="a", check_command="true", paths=["widget.py"]),
            Upgrade(id=2, title="b", check_command="true", paths=["widget.py"]),
            Upgrade(id=3, title="c", check_command="true", paths=["widget.py"]),
        ]
    )


def test_writer_timeout_is_retry_not_abort(fixture_repo):
    class Boom:
        mock = False

        def chat(self, messages, **kwargs):
            raise TimeoutError("timed out")

    result = Writer(Boom(), fixture_repo).apply_job("edit widget", _brief(), "")
    assert result.written == []
    assert result.message == "timeout"
    assert any("timed out" in note for note in result.refused)
    assert (fixture_repo / "widget.py").is_file()
