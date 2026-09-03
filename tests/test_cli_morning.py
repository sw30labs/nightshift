from __future__ import annotations

from nightshift import cli
from nightshift.runner import run_night


def test_morning_and_status(fixture_repo, mock_settings, ns_home, capsys):
    run_night(fixture_repo, mock_settings, explicit=True)
    code = cli.main(["morning", str(fixture_repo)])
    out = capsys.readouterr().out
    assert code == 0
    assert "night/" in out
    assert "merge --no-ff" in out
    assert "test_add" in out or "Make test_add" in out
    code = cli.main(["morning", str(fixture_repo), "--diff"])
    out = capsys.readouterr().out
    assert "widget.py" in out
    code = cli.main(["status"])
    out = capsys.readouterr().out
    assert "state" in out
    code = cli.main(["status", "--json"])
    out = capsys.readouterr().out
    assert out.strip().startswith("{")
    # idle home
    import os

    os.environ["NIGHTSHIFT_HOME"] = str(ns_home / "empty")
    (ns_home / "empty").mkdir()
    code = cli.main(["status"])
    assert code == 1
    err = capsys.readouterr()
    assert "no overnight run recorded" in (err.out + err.err)
