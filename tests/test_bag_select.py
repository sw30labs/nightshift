from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from nightshift import cli
from nightshift.bag import load_bag, select_bag
from nightshift.config import Settings
from nightshift.demo import seed_widget
from nightshift.gitops import checkout_night_branch, current_branch, git, last_commit_unix
from nightshift.models import SafetyError
from nightshift.safety import is_nightshift_repo, tree_state


def seed_fake_nightshift(dest: Path) -> Path:
    repo = seed_widget(dest)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "nightshift"\nversion = "0.0.0"\n', encoding="utf-8"
    )
    (repo / "src" / "nightshift").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "nightshift" / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "fake nightshift marker")
    assert is_nightshift_repo(repo)
    return repo


def _settings(ns_home: Path, roots: list[Path], **over) -> Settings:
    kw = dict(
        mock=True,
        observe=False,
        home=ns_home,
        roots=list(roots),
        max_turns=4,
        stall_after=4,
        check_timeout=30,
        push=False,
        now_fn=lambda: datetime(2026, 9, 4, 2, 0),
    )
    kw.update(over)
    return Settings(**kw)


def test_last_commit_unix(fixture_repo):
    ts = last_commit_unix(fixture_repo)
    assert ts > 0


def test_select_bag_meta_first_and_writes_idle(tmp_path, ns_home):
    meta = seed_fake_nightshift(tmp_path / "nightshift")
    a = seed_widget(tmp_path / "alpha")
    seed_widget(tmp_path / "beta")
    plan = select_bag(_settings(ns_home, [tmp_path]), size=2)
    assert [t.role for t in plan.targets] == ["meta", "portfolio"]
    assert plan.targets[0].path == meta
    assert plan.targets[1].name in {"alpha", "beta"}
    assert a.name == "alpha"
    bag = load_bag(ns_home)
    assert bag["state"] == "idle"
    assert bag["runner_pid"] is None
    assert len(bag["targets"]) == 2


def test_skip_meta_drops_nightshift_from_slots(tmp_path, ns_home):
    seed_fake_nightshift(tmp_path / "nightshift")
    seed_widget(tmp_path / "alpha")
    seed_widget(tmp_path / "beta")
    plan = select_bag(_settings(ns_home, [tmp_path]), size=2, skip_meta=True)
    names = [t.name for t in plan.targets]
    assert "nightshift" not in names
    assert all(t.role == "portfolio" for t in plan.targets)
    assert len(plan.targets) == 2


def test_meta_last_puts_meta_at_end(tmp_path, ns_home):
    meta = seed_fake_nightshift(tmp_path / "nightshift")
    seed_widget(tmp_path / "alpha")
    plan = select_bag(_settings(ns_home, [tmp_path]), size=2, meta_last=True)
    assert plan.targets[-1].path == meta
    assert plan.targets[-1].role == "meta"
    assert plan.targets[0].role == "portfolio"


def test_dirty_skip_uses_tree_state_not_junk(tmp_path, ns_home):
    seed_fake_nightshift(tmp_path / "nightshift")
    dirty = seed_widget(tmp_path / "dirty")
    (dirty / "scratch.py").write_text("x\n")
    junk = seed_widget(tmp_path / "junked")
    (junk / "htmlcov").mkdir()
    (junk / "htmlcov" / "index.html").write_text("x\n")
    assert tree_state(dirty).dirty
    assert not tree_state(junk).dirty
    plan = select_bag(_settings(ns_home, [tmp_path]), size=3)
    names = {t.name for t in plan.targets}
    skipped = {t.name: t.skip_reason for t in plan.skipped}
    assert "dirty" in skipped
    assert skipped["dirty"] == "dirty tree"
    assert "junked" in names
    assert "dirty" not in names


def test_night_branch_skipped(tmp_path, ns_home):
    seed_fake_nightshift(tmp_path / "nightshift")
    other = seed_widget(tmp_path / "onnight")
    checkout_night_branch(other, datetime(2026, 9, 4, 1, 0))
    assert current_branch(other).startswith("night/")
    plan = select_bag(_settings(ns_home, [tmp_path]), size=2)
    skipped = {t.name: t.skip_reason for t in plan.skipped}
    assert skipped.get("onnight") == "on night branch"


def test_dirty_meta_fills_from_others(tmp_path, ns_home):
    meta = seed_fake_nightshift(tmp_path / "nightshift")
    (meta / "scratch.py").write_text("x\n")
    seed_widget(tmp_path / "alpha")
    seed_widget(tmp_path / "beta")
    plan = select_bag(_settings(ns_home, [tmp_path]), size=2)
    assert all(t.role == "portfolio" for t in plan.targets)
    assert len(plan.targets) == 2
    assert {t.name for t in plan.targets} == {"alpha", "beta"}


def test_prior_skip_and_liked(tmp_path, ns_home):
    seed_fake_nightshift(tmp_path / "nightshift")
    seed_widget(tmp_path / "alpha")
    seed_widget(tmp_path / "beta")
    seed_widget(tmp_path / "scratch")
    (ns_home / "prior.json").write_text(
        json.dumps({"liked": ["beta"], "skip": ["scratch"]}), encoding="utf-8"
    )
    plan = select_bag(_settings(ns_home, [tmp_path]), size=2)
    names = [t.name for t in plan.targets]
    assert names[0] == "nightshift"
    assert names[1] == "beta"
    skipped = {t.name: t.skip_reason for t in plan.skipped}
    assert skipped.get("scratch") == "prior skip"


def test_size_clamped_to_3(tmp_path, ns_home):
    seed_fake_nightshift(tmp_path / "nightshift")
    for i in range(5):
        seed_widget(tmp_path / f"r{i}")
    plan = select_bag(_settings(ns_home, [tmp_path]), size=9)
    assert plan.size == 3
    assert len(plan.targets) == 3


def test_select_does_not_call_critic(tmp_path, ns_home, monkeypatch):
    seed_fake_nightshift(tmp_path / "nightshift")
    seed_widget(tmp_path / "alpha")

    def boom(*a, **k):
        raise AssertionError("critic should not run during bag select")

    monkeypatch.setattr("nightshift.llm.Critic.propose_brief", boom)
    monkeypatch.setattr("nightshift.runner.Critic", lambda *a, **k: boom())
    plan = select_bag(_settings(ns_home, [tmp_path]))
    assert plan.targets


def test_cli_bag_table_and_exit(tmp_path, ns_home, capsys):
    seed_fake_nightshift(tmp_path / "nightshift")
    seed_widget(tmp_path / "alpha")
    code = cli.main(["bag", "--roots", str(tmp_path), "--mock", "--no-observe"])
    out = capsys.readouterr().out
    assert code == 0
    assert "meta" in out
    assert "nightshift" in out
    empty = tmp_path / "empty-root"
    empty.mkdir()
    code = cli.main(["bag", "--roots", str(empty), "--mock", "--no-observe"])
    assert code == 1


def test_cli_bag_skip_meta(tmp_path, ns_home, capsys):
    seed_fake_nightshift(tmp_path / "nightshift")
    seed_widget(tmp_path / "alpha")
    seed_widget(tmp_path / "beta")
    code = cli.main(
        ["bag", "--roots", str(tmp_path), "--mock", "--skip-meta", "--size", "2"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "nightshift" not in out
    assert "alpha" in out and "beta" in out


def test_package_checkout_fills_meta_when_missing_from_roots(tmp_path, ns_home, monkeypatch):
    seed_widget(tmp_path / "alpha")
    meta = seed_fake_nightshift(tmp_path.parent / f"{tmp_path.name}-pkg-ns")
    monkeypatch.setattr("nightshift.bag.package_checkout", lambda: meta)
    try:
        plan = select_bag(_settings(ns_home, [tmp_path]), size=2)
        assert plan.targets[0].path == meta.resolve()
        assert plan.targets[0].role == "meta"
    finally:
        import shutil

        shutil.rmtree(meta, ignore_errors=True)


def test_package_checkout_is_meta_fallback_not_a_second_target(tmp_path, ns_home, monkeypatch):
    """A Nightshift under roots is meta; package_checkout outside roots must not join as portfolio."""
    import shutil

    fake = seed_fake_nightshift(tmp_path / "nightshift")
    seed_widget(tmp_path / "alpha")
    real = seed_fake_nightshift(tmp_path.parent / f"{tmp_path.name}-outside-ns")
    monkeypatch.setattr("nightshift.bag.package_checkout", lambda: real)
    try:
        plan = select_bag(_settings(ns_home, [tmp_path]), size=3)
        paths = {t.path.resolve() for t in plan.targets}
        skipped = {t.path.resolve() for t in plan.skipped}
        assert fake.resolve() in paths
        assert real.resolve() not in paths
        assert real.resolve() not in skipped
    finally:
        shutil.rmtree(real, ignore_errors=True)


def test_select_refuses_live_bag(tmp_path, ns_home):
    from nightshift.bag import save_bag
    import os

    seed_widget(tmp_path / "alpha")
    save_bag(
        ns_home,
        {"state": "running", "runner_pid": os.getpid(), "targets": []},
    )
    with pytest.raises(SafetyError, match="bag is already running"):
        select_bag(_settings(ns_home, [tmp_path]))
