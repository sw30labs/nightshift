from __future__ import annotations

import json
import subprocess
from pathlib import Path

from nightshift.graph import read_snapshot
from nightshift.llm import CRITIC_BRIEF_SYSTEM, CRITIC_JOB_SYSTEM, Writer
from nightshift.models import Brief, Upgrade


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _tiny_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "secret-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "nightshift@localhost")
    _git(repo, "config", "user.name", "Nightshift")
    (repo / ".gitignore").write_text(".env\nignored.txt\n")
    (repo / "README.md").write_text("# toy\n")
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "add", ".gitignore", "README.md", "app.py")
    _git(repo, "commit", "-m", "init")
    (repo / ".env").write_text("OPENAI_API_KEY=sk-live-not-for-models\n")
    (repo / "ignored.txt").write_text("also-secret\n")
    (repo / ".env.example").write_text("OPENAI_API_KEY=\n")
    return repo


def _brief() -> Brief:
    return Brief.freeze(
        [
            Upgrade(id=1, title="a", check_command="true", paths=["app.py"]),
            Upgrade(id=2, title="b", check_command="true", paths=["README.md"]),
            Upgrade(id=3, title="c", check_command="true", paths=["app.py"]),
        ]
    )


def test_snapshot_skips_env_and_gitignore(tmp_path):
    repo = _tiny_repo(tmp_path)
    snap = read_snapshot(repo)
    assert "sk-live-not-for-models" not in snap
    assert "also-secret" not in snap
    assert "## file .env\n" not in snap
    assert "app.py" in snap
    assert ".env.example" in snap


def test_writer_skips_env_and_keeps_going(tmp_path):
    repo = _tiny_repo(tmp_path)
    before = (repo / ".env").read_text(encoding="utf-8")

    class Fake:
        mock = False

        def chat(self, messages, **kwargs):
            return json.dumps(
                {
                    "files": [
                        {"path": ".env", "content": "HACKED=1\n"},
                        {"path": "app.py", "content": "x = 2\n"},
                    ],
                    "message": "mixed",
                }
            )

    result = Writer(Fake(), repo).apply_job("touch .env and app", _brief(), "")
    assert "app.py" in result.written
    assert ".env" not in result.written
    assert any(".env" in note for note in result.refused)
    assert (repo / "app.py").read_text(encoding="utf-8") == "x = 2\n"
    assert (repo / ".env").read_text(encoding="utf-8") == before


def test_critic_prompt_forbids_secret_rotation():
    blob = CRITIC_BRIEF_SYSTEM + CRITIC_JOB_SYSTEM
    assert ".env" in blob
    assert "secret" in blob.lower()
