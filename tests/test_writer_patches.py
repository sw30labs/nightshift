from __future__ import annotations

import json

from nightshift.llm import MAX_FULL_FILE_CHARS, Writer
from nightshift.models import Brief, Upgrade


def _brief() -> Brief:
    return Brief.freeze(
        [
            Upgrade(id=1, title="a", check_command="true", paths=["README.md"]),
            Upgrade(id=2, title="b", check_command="true", paths=["tiny.py"]),
            Upgrade(id=3, title="c", check_command="true", paths=["widget.py"]),
        ]
    )


def test_writer_applies_unique_hunk(fixture_repo):
    readme = fixture_repo / "README.md"
    readme.write_text("# Demo\n\nRun with -p 8000:8000\n\nMore text.\n")

    class PatchClient:
        mock = False

        def chat(self, messages, **kwargs):
            return json.dumps(
                {
                    "patches": [
                        {
                            "path": "README.md",
                            "old": "-p 8000:8000",
                            "new": "-p 8080:8080",
                        }
                    ],
                    "files": [],
                    "message": "port 8080",
                }
            )

    result = Writer(PatchClient(), fixture_repo).apply_job("fix ports", _brief(), "")
    assert "README.md" in result.written
    assert "-p 8080:8080" in readme.read_text()
    assert "-p 8000:8000" not in readme.read_text()


def test_writer_refuses_oversized_full_file(fixture_repo):
    readme = fixture_repo / "README.md"
    original = "# Keep me\n" + ("x" * 200)
    readme.write_text(original)

    class FatClient:
        mock = False

        def chat(self, messages, **kwargs):
            return json.dumps(
                {
                    "files": [
                        {
                            "path": "README.md",
                            "content": "truncated " + ("y" * (MAX_FULL_FILE_CHARS + 10)),
                        }
                    ],
                    "message": "dump",
                }
            )

    result = Writer(FatClient(), fixture_repo).apply_job("rewrite readme", _brief(), "")
    assert result.written == []
    assert any("patches[]" in note for note in result.refused)
    assert readme.read_text() == original


def test_writer_missing_path_patch_creates_file(fixture_repo):
    class PatchClient:
        mock = False

        def chat(self, messages, **kwargs):
            return json.dumps(
                {
                    "patches": [
                        {
                            "path": "tests/test_cli_version.py",
                            "old": "does-not-matter",
                            "new": "def test_version():\n    assert True\n",
                        }
                    ],
                    "files": [],
                    "message": "create version tests",
                }
            )

    result = Writer(PatchClient(), fixture_repo).apply_job("add version tests", _brief(), "")
    assert "tests/test_cli_version.py" in result.written
    text = (fixture_repo / "tests" / "test_cli_version.py").read_text()
    assert "def test_version" in text


def test_writer_tells_model_when_job_path_missing(fixture_repo):
    class Capture:
        mock = False
        seen = ""

        def chat(self, messages, **kwargs):
            self.seen = messages[1]["content"]
            return json.dumps(
                {
                    "files": [
                        {"path": "tests/test_new.py", "content": "def test_x():\n    assert True\n"}
                    ],
                    "message": "ok",
                }
            )

    brief = Brief.freeze(
        [
            Upgrade(id=1, title="a", check_command="true", paths=["tests/test_new.py"]),
            Upgrade(id=2, title="b", check_command="true", paths=["widget.py"]),
            Upgrade(id=3, title="c", check_command="true", paths=["widget.py"]),
        ]
    )
    client = Capture()
    result = Writer(client, fixture_repo).apply_job("create tests", brief, "")
    assert "tests/test_new.py" in client.seen
    assert "never patches[]" in client.seen
    assert "tests/test_new.py" in result.written


def test_writer_missing_path_patch_refuses_huge_new(fixture_repo):
    class Fat:
        mock = False

        def chat(self, messages, **kwargs):
            return json.dumps(
                {
                    "patches": [
                        {"path": "tests/test_huge.py", "old": "x", "new": "y" * (MAX_FULL_FILE_CHARS + 1)}
                    ],
                    "message": "nope",
                }
            )

    result = Writer(Fat(), fixture_repo).apply_job("add huge", _brief(), "")
    assert result.written == []
    assert not (fixture_repo / "tests" / "test_huge.py").exists()
    assert any("files[] content" in note for note in result.refused)
