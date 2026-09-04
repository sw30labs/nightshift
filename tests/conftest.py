from __future__ import annotations

from pathlib import Path

import pytest

from nightshift.config import Settings
from nightshift.demo import seed_widget


@pytest.fixture(autouse=True)
def _no_package_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    # Design note N7: the suite never treats the operator's own checkout as a
    # meta target. A test that needs one seeds a fake Nightshift repo under tmp.
    monkeypatch.setattr("nightshift.cmm.package_checkout", lambda: None)
    monkeypatch.setattr("nightshift.bag.package_checkout", lambda: None)


@pytest.fixture
def ns_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "ns-home"
    home.mkdir()
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(home))
    return home


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    return seed_widget(tmp_path / "widget")


@pytest.fixture
def mock_settings(ns_home: Path) -> Settings:
    return Settings(
        mock=True,
        observe=False,
        home=ns_home,
        max_turns=12,
        stall_after=12,
        check_timeout=30,
        push=False,
    )
