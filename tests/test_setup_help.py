from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "setup_and_run.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_setup_help_exits_zero_and_prints_usage(tmp_path: Path) -> None:
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Usage:" in proc.stdout
    assert "setup_and_run.sh" in proc.stdout
    assert "--help" in proc.stdout
