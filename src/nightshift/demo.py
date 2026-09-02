"""Tiny failing git repo used by tests and `nightshift serve --demo`."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

WIDGET_PY = """\
def add(a, b):
    return a + b + 1
"""

TEST_WIDGET = """\
from pathlib import Path

from widget import add


def test_add():
    assert add(1, 1) == 2


def test_greet():
    from widget import greet

    assert greet("Nic") == "hello Nic"


def test_version():
    assert Path("VERSION").read_text().strip() == "1.0.0"
"""

README = """\
# widget

A tiny library. `add` should return the sum. `greet` should return
`hello <name>`. `VERSION` should contain `1.0.0`.
"""


def seed_widget(dest: Path) -> Path:
    dest = dest.expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "widget.py").write_text(WIDGET_PY, encoding="utf-8")
    (dest / "README.md").write_text(README, encoding="utf-8")
    (dest / "tests").mkdir(exist_ok=True)
    (dest / "tests" / "test_widget.py").write_text(TEST_WIDGET, encoding="utf-8")
    (dest / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (dest / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n.nightshift/events.jsonl\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "Nightshift Fixture")
    env.setdefault("GIT_AUTHOR_EMAIL", "fixture@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "Nightshift Fixture")
    env.setdefault("GIT_COMMITTER_EMAIL", "fixture@localhost")
    subprocess.run(["git", "init", "-b", "main"], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Nightshift Fixture"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@localhost"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "widget: failing suite on main"],
        cwd=dest,
        check=True,
        capture_output=True,
        env=env,
    )
    return dest.resolve()
