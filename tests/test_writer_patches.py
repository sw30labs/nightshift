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
