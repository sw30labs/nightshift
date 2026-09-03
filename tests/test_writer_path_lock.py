from __future__ import annotations

import json

from nightshift.llm import CRITIC_JOB_SYSTEM, CRITIC_SCORE_SYSTEM, WRITER_SYSTEM, Writer
from nightshift.models import Brief, Upgrade


def _brief(paths_a, paths_b=None):
    return Brief.freeze(
        [
            Upgrade(id=1, title="a", check_command="true", paths=list(paths_a)),
            Upgrade(id=2, title="b", check_command="true", paths=list(paths_b or ["widget.py"])),
        ]
    )


class _Client:
    mock = False

    def __init__(self, payload):
        self.payload = payload

    def chat(self, messages, **kwargs):
        return json.dumps(self.payload)


def test_patch_outside_job_paths_is_rejected(fixture_repo):
    target = fixture_repo / "src" / "nightshift" / "ledger.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    original = "ok = True\n"
    target.write_text(original)
    client = _Client(
        {
            "patches": [
                {"path": "src/nightshift/ledger.py", "old": "ok = True", "new": "from mo"}
            ],
            "files": [],
            "message": "wreck ledger",
        }
    )
    result = Writer(client, fixture_repo).apply_job("host checks", _brief(["tests/test_host.py"]), "")
    assert result.written == []
    assert any("outside job paths" in note for note in result.refused)
    assert target.read_text() == original


def test_files_outside_job_paths_is_rejected(fixture_repo):
    client = _Client(
        {
            "files": [{"path": "src/nightshift/ledger.py", "content": "from mo\n"}],
            "message": "new ledger",
        }
    )
    result = Writer(client, fixture_repo).apply_job("host checks", _brief(["tests/test_host.py"]), "")
    assert result.written == []
    assert any("outside job paths" in note for note in result.refused)
    assert not (fixture_repo / "src" / "nightshift" / "ledger.py").exists()


def test_patch_inside_job_paths_still_works(fixture_repo):
    readme = fixture_repo / "README.md"
    readme.write_text("# Demo\nport 8000\n")
    client = _Client(
        {
            "patches": [{"path": "README.md", "old": "port 8000", "new": "port 8080"}],
            "files": [],
            "message": "port",
        }
    )
    result = Writer(client, fixture_repo).apply_job("fix port", _brief(["README.md"]), "")
    assert "README.md" in result.written
    assert "port 8080" in readme.read_text()


def test_files_inside_job_paths_still_works(fixture_repo):
    client = _Client(
        {
            "files": [{"path": "tests/test_host.py", "content": "def test_ok():\n    assert True\n"}],
            "message": "add test",
        }
    )
    result = Writer(client, fixture_repo).apply_job(
        "add host test", _brief(["tests/test_host.py"]), ""
    )
    assert "tests/test_host.py" in result.written
    assert "def test_ok" in (fixture_repo / "tests" / "test_host.py").read_text()


def test_empty_paths_rejects_every_write(fixture_repo):
    widget = fixture_repo / "widget.py"
    before = widget.read_text()
    client = _Client(
        {
            "files": [{"path": "widget.py", "content": "def add(a, b):\n    return 0\n"}],
            "message": "nope",
        }
    )
    result = Writer(client, fixture_repo).apply_job("empty", _brief([]), "")
    assert result.written == []
    assert any("paths[] is empty" in note for note in result.refused)
    assert widget.read_text() == before


def test_other_job_paths_are_not_writable(fixture_repo):
    widget = fixture_repo / "widget.py"
    before = widget.read_text()
    client = _Client(
        {
            "files": [{"path": "widget.py", "content": "def add(a, b):\n    return 0\n"}],
            "message": "side effect",
        }
    )
    result = Writer(client, fixture_repo).apply_job(
        "tests only", _brief(["tests/test_host.py"], ["widget.py"]), ""
    )
    assert result.written == []
    assert widget.read_text() == before


def test_traversal_and_absolute_writes_are_rejected(fixture_repo):
    ledger = fixture_repo / "widget.py"
    before = ledger.read_text()
    client = _Client(
        {
            "files": [
                {"path": "../widget.py", "content": "hack\n"},
                {"path": "/tmp/x.py", "content": "hack\n"},
            ],
            "message": "escape",
        }
    )
    result = Writer(client, fixture_repo).apply_job("escape", _brief(["widget.py"]), "")
    assert result.written == []
    assert any("traversal" in note or "absolute" in note for note in result.refused)
    assert ledger.read_text() == before


def test_prompts_forbid_writes_outside_job_paths():
    blob = WRITER_SYSTEM + CRITIC_JOB_SYSTEM + CRITIC_SCORE_SYSTEM
    assert "paths[]" in blob
    assert "outside" in blob.lower()
