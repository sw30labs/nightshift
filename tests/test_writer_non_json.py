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


def test_writer_non_json_is_retry_not_abort(fixture_repo):
    class Prose:
        mock = False

        def chat(self, messages, **kwargs):
            return "thinking, no braces"

    result = Writer(Prose(), fixture_repo).apply_job("edit widget", _brief(), "")
    assert result.written == []
    assert result.message == "non-json"
    assert any("non-JSON" in note for note in result.refused)
    assert (fixture_repo / "widget.py").is_file()


def test_writer_non_json_retries_then_writes(fixture_repo):
    class Flaky:
        mock = False
        n = 0

        def chat(self, messages, **kwargs):
            self.n += 1
            if self.n < 3:
                return "still thinking"
            return '{"files": [{"path": "widget.py", "content": "def add(a, b):\\n    return a + b\\n"}], "message": "ok"}'

    client = Flaky()
    result = Writer(client, fixture_repo).apply_job("edit widget", _brief(), "")
    assert client.n == 3
    assert "widget.py" in result.written
    assert "return a + b" in (fixture_repo / "widget.py").read_text()
