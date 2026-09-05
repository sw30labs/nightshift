from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nightshift import cli
from nightshift.bag import (
    acquire_bag,
    assert_shift_idle,
    bag_exit_code,
    load_bag,
    pid_alive,
    recover_stale_bag,
    run_bag,
    save_bag,
    select_bag,
)
from nightshift.config import Settings
from nightshift.demo import seed_widget
from nightshift.forum import load_forum
from nightshift.models import SafetyError
from nightshift.runner import NightReport, run_night


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


def test_pid_alive_self_is_alive():
    assert pid_alive(os.getpid()) is True
    assert pid_alive(None) is False
    assert pid_alive(os.getpid() + 10_000_000) is False


def test_recover_stale_dead_pid_not_self(ns_home, monkeypatch):
    save_bag(
        ns_home,
        {
            "state": "running",
            "runner_pid": 424242,
            "targets": [{"state": "queued", "name": "a"}, {"state": "running", "name": "b"}],
        },
    )
    monkeypatch.setattr("nightshift.bag.pid_alive", lambda pid, self_pid=None: False)
    recover_stale_bag(ns_home)
    bag = load_bag(ns_home)
    assert bag["state"] == "halted"
    assert bag["runner_pid"] is None
    assert all(t["state"] == "skipped" for t in bag["targets"])


def test_deck_pid_bag_is_not_stale_recovered(ns_home):
    save_bag(
        ns_home,
        {
            "state": "running",
            "runner_pid": os.getpid(),
            "targets": [{"state": "queued", "name": "a"}],
        },
    )
    recover_stale_bag(ns_home)
    assert load_bag(ns_home)["state"] == "running"
    assert load_bag(ns_home)["targets"][0]["state"] == "queued"


def test_acquire_bag_serializes(ns_home, monkeypatch):
    alive = {101, 202}

    def fake_alive(pid, self_pid=None):
        try:
            return int(pid) in alive
        except (TypeError, ValueError):
            return False

    monkeypatch.setattr("nightshift.bag.pid_alive", fake_alive)
    acquire_bag(ns_home, {"state": "running", "runner_pid": 101, "targets": []}, self_pid=101)
    with pytest.raises(SafetyError, match="bag is already running"):
        acquire_bag(
            ns_home, {"state": "running", "runner_pid": 202, "targets": []}, self_pid=202
        )


def test_assert_shift_idle_allow_self(ns_home):
    save_bag(ns_home, {"state": "running", "runner_pid": os.getpid(), "targets": []})
    with pytest.raises(SafetyError, match="bag is already running"):
        assert_shift_idle(ns_home, allow_self=False)
    assert_shift_idle(ns_home, allow_self=True)


def test_run_night_inner_idle_own_bag(ns_home, fixture_repo):
    save_bag(ns_home, {"state": "running", "runner_pid": os.getpid(), "targets": []})
    settings = Settings(mock=True, observe=False, home=ns_home, max_turns=2, stall_after=2)
    with pytest.raises(SafetyError, match="bag is already running"):
        run_night(fixture_repo, settings, explicit=True, allow_self_bag=False)
    report = run_night(fixture_repo, settings, explicit=True, allow_self_bag=True)
    assert report.halt_reason == "remaining_zero"


def test_run_bag_isolates_failure_and_still_publishes(tmp_path, ns_home, monkeypatch):
    seed_widget(tmp_path / "alpha")
    seed_widget(tmp_path / "beta")
    (ns_home / "prior.json").write_text('{"liked": ["alpha"], "skip": []}', encoding="utf-8")
    settings = _settings(ns_home, [tmp_path], skip_meta=True)
    plan = select_bag(settings, size=2, skip_meta=True)
    assert [t.name for t in plan.targets][:2] == ["alpha", "beta"]
    real = run_night

    def flaky(repo, settings, *, explicit=True, allow_self_bag=False):
        if Path(repo).name == "alpha":
            raise RuntimeError("boom alpha")
        return real(repo, settings, explicit=explicit, allow_self_bag=allow_self_bag)

    monkeypatch.setattr("nightshift.runner.run_night", flaky)
    result = run_bag(plan, settings)
    assert result["state"] == "done"
    by_name = {t["name"]: t for t in result["targets"]}
    assert by_name["alpha"]["state"] == "error"
    assert by_name["beta"]["state"] == "done"
    forum = load_forum(ns_home)
    beta_id = plan.targets[1].repo_id
    assert any(n.get("repo_id") == beta_id for n in forum.get("nights") or [])
    assert any("boom alpha" in str(e.get("error") or "") for e in forum.get("errors") or [])


def test_halt_bag_skips_rest(tmp_path, ns_home, monkeypatch):
    seed_widget(tmp_path / "alpha")
    seed_widget(tmp_path / "beta")
    (ns_home / "prior.json").write_text('{"liked": ["alpha"], "skip": []}', encoding="utf-8")
    settings = _settings(ns_home, [tmp_path], skip_meta=True)
    plan = select_bag(settings, size=2, skip_meta=True)
    real = run_night
    calls: list[str] = []

    def halt_after_first(repo, settings, *, explicit=True, allow_self_bag=False):
        calls.append(Path(repo).name)
        bag = load_bag(settings.home)
        bag["halt_bag"] = True
        save_bag(settings.home, bag)
        return real(repo, settings, explicit=explicit, allow_self_bag=allow_self_bag)

    monkeypatch.setattr("nightshift.runner.run_night", halt_after_first)
    result = run_bag(plan, settings)
    assert calls == ["alpha"]
    assert result["state"] == "halted"
    by_name = {t["name"]: t for t in result["targets"]}
    assert by_name["beta"]["state"] == "skipped"
    assert by_name["beta"]["halt_reason"] == "bag_halted"


def test_clock_short_skips_without_fake_items(tmp_path, ns_home):
    seed_widget(tmp_path / "alpha")
    seed_widget(tmp_path / "beta")
    now = datetime(2026, 9, 4, 5, 40)
    settings = _settings(
        ns_home,
        [tmp_path],
        skip_meta=True,
        now_fn=lambda: now,
        halt_at="06:00",
        bag_min_minutes=30,
    )
    plan = select_bag(settings, size=2, skip_meta=True)
    result = run_bag(plan, settings)
    assert all(t["state"] == "skipped" for t in result["targets"])
    assert all(t["halt_reason"] == "clock_short" for t in result["targets"])
    assert not (load_forum(ns_home).get("nights") or [])


def test_keyboard_interrupt_halts_bag(tmp_path, ns_home, monkeypatch):
    seed_widget(tmp_path / "alpha")
    seed_widget(tmp_path / "beta")
    settings = _settings(ns_home, [tmp_path], skip_meta=True)
    plan = select_bag(settings, size=2, skip_meta=True)

    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr("nightshift.runner.run_night", boom)
    with pytest.raises(KeyboardInterrupt):
        run_bag(plan, settings)
    bag = load_bag(ns_home)
    assert bag["state"] == "halted"
    assert bag["halt_bag"] is True
    assert bag["runner_pid"] is None


def test_observe_start_failure_releases_bag_and_skips_targets(tmp_path, ns_home, monkeypatch):
    seed_widget(tmp_path / "alpha")
    settings = _settings(ns_home, [tmp_path], skip_meta=True, observe=True)
    plan = select_bag(settings, size=1, skip_meta=True)
    stopped = []

    def boom(**kwargs):
        raise RuntimeError("observer failed during startup")

    monkeypatch.setattr("nightshift.bag.observe_start", boom)
    monkeypatch.setattr("nightshift.bag.stop_active", lambda: stopped.append(True))
    with pytest.raises(RuntimeError, match="observer failed"):
        run_bag(plan, settings)
    bag = load_bag(ns_home)
    assert bag["state"] == "error"
    assert bag["runner_pid"] is None
    assert bag["targets"][0]["state"] == "skipped"
    assert bag["targets"][0]["halt_reason"] == "error"
    assert stopped == [True]
    assert_shift_idle(ns_home)


def test_reserved_bag_keeps_halt_requested_before_worker_starts(ns_home):
    reservation = {
        "bag_id": "b-reserved", "state": "running", "runner_pid": os.getpid(),
        "halt_bag": True, "targets": [],
    }
    save_bag(ns_home, reservation)
    acquire_bag(ns_home, {**reservation, "halt_bag": False, "current_index": -1})
    assert load_bag(ns_home)["halt_bag"] is True
    with pytest.raises(SafetyError, match="bag is already running"):
        acquire_bag(ns_home, {**reservation, "current_index": -1})


def test_same_process_cannot_replace_different_reserved_bag(ns_home):
    save_bag(ns_home, {
        "bag_id": "b-existing", "state": "running", "runner_pid": os.getpid(), "targets": [],
    })
    with pytest.raises(SafetyError, match="bag is already running"):
        acquire_bag(ns_home, {
            "bag_id": "b-another", "state": "running", "runner_pid": os.getpid(), "targets": [],
        })
    assert load_bag(ns_home)["bag_id"] == "b-existing"


def test_cmd_run_refuses_while_bag_live(fixture_repo, ns_home, capsys):
    save_bag(
        ns_home,
        {"state": "running", "runner_pid": os.getpid(), "targets": [{"state": "queued"}]},
    )
    code = cli.main(["run", str(fixture_repo), "--mock", "--no-observe"])
    err = capsys.readouterr()
    assert code == 1
    assert "bag is already running" in (err.out + err.err)


def test_cmd_run_dry_run_skips_lock(fixture_repo, ns_home):
    save_bag(
        ns_home,
        {"state": "running", "runner_pid": os.getpid(), "targets": [{"state": "queued"}]},
    )
    code = cli.main(["run", str(fixture_repo), "--mock", "--dry-run", "--no-observe"])
    assert code == 0


def test_status_bag_and_halt_bag(ns_home, capsys):
    save_bag(
        ns_home,
        {
            "state": "running",
            "bag_id": "b-test",
            "runner_pid": os.getpid(),
            "halt_bag": False,
            "targets": [{"name": "alpha", "role": "portfolio", "state": "queued"}],
        },
    )
    code = cli.main(["status", "--bag"])
    out = capsys.readouterr().out
    assert code == 0
    assert "bag       running" in out
    assert "alpha" in out
    code = cli.main(["halt"])
    assert code == 0
    assert load_bag(ns_home)["halt_bag"] is True


def test_settings_copy_does_not_leak_deadline(tmp_path, ns_home, monkeypatch):
    seed_widget(tmp_path / "alpha")
    settings = _settings(ns_home, [tmp_path], skip_meta=True)
    assert settings.halt_deadline is None
    plan = select_bag(settings, size=1, skip_meta=True)

    def fake_run(repo, night_settings, *, explicit=True, allow_self_bag=False):
        assert night_settings.halt_deadline is not None
        assert settings.halt_deadline is None
        assert night_settings.bag_id == plan.bag_id
        return NightReport(
            repo=Path(repo),
            branch="night/2026-09-04",
            main_ref="main",
            main_sha="abc",
            remaining_count=0,
            halt_reason="remaining_zero",
            main_untouched=True,
        )

    monkeypatch.setattr("nightshift.runner.run_night", fake_run)
    run_bag(plan, settings)
    assert settings.halt_deadline is None


def test_forum_env_off_skips_stub(tmp_path, ns_home, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_FORUM", "0")
    seed_widget(tmp_path / "alpha")
    settings = _settings(ns_home, [tmp_path], skip_meta=True)
    plan = select_bag(settings, size=1, skip_meta=True)

    def boom(*a, **k):
        raise RuntimeError("no forum")

    monkeypatch.setattr("nightshift.runner.run_night", boom)
    result = run_bag(plan, settings)
    assert result["state"] == "done"
    assert not (ns_home / "forum.json").exists()


def test_bag_exit_codes():
    assert bag_exit_code({"interrupted": True, "targets": [{}]}) == 130
    assert bag_exit_code({"main_touched": True, "targets": [{"state": "done", "remaining_count": 0}]}) == 3
    assert bag_exit_code({"targets": [{"state": "error"}]}) == 2
    assert bag_exit_code({"targets": [{"state": "skipped"}]}) == 2
    assert bag_exit_code({"targets": [{"state": "done", "remaining_count": 1}]}) == 2
    assert bag_exit_code({"targets": [{"state": "done", "remaining_count": 0}]}) == 0
    assert bag_exit_code({"targets": []}) == 1


def test_run_bag_ralph_phases_and_observe_once(tmp_path, ns_home, monkeypatch):
    seed_widget(tmp_path / "alpha")
    seed_widget(tmp_path / "beta")
    (ns_home / "prior.json").write_text('{"liked": ["alpha"], "skip": []}', encoding="utf-8")
    settings = _settings(ns_home, [tmp_path], skip_meta=True, observe=True, loopscope_port=17996)
    plan = select_bag(settings, size=2, skip_meta=True)
    starts: list[str] = []
    real_start = __import__("nightshift.observe", fromlist=["start"]).start

    def wrap_start(**kwargs):
        starts.append(str(kwargs.get("jsonl") or ""))
        return real_start(**kwargs)

    monkeypatch.setattr("nightshift.bag.observe_start", wrap_start)
    monkeypatch.setattr("nightshift.runner.observe_start", wrap_start)

    def fake_run(repo, night_settings, *, explicit=True, allow_self_bag=False):
        assert night_settings.observe is False
        return NightReport(
            repo=Path(repo),
            branch="night/2026-09-04",
            main_ref="main",
            main_sha="abc",
            remaining_count=0,
            halt_reason="remaining_zero",
            main_untouched=True,
        )

    monkeypatch.setattr("nightshift.runner.run_night", fake_run)
    seen: list[str] = []
    real_ralph = __import__("nightshift.observe", fromlist=["ralph_loop"]).ralph_loop

    def wrap_ralph(objective, **kwargs):
        seen.append(objective)
        assert kwargs.get("phases") == ["select", "night", "forum"]
        assert kwargs.get("stall_after") == 0
        return real_ralph(objective, **kwargs)

    monkeypatch.setattr("nightshift.bag.ralph_loop", wrap_ralph)
    result = run_bag(plan, settings)
    assert result["state"] == "done"
    assert seen == ["tonight's bag remaining"]
    assert len(starts) == 1
    assert starts[0].endswith("bag-events.jsonl")


def test_two_nights_observe_releases_port(tmp_path, ns_home, monkeypatch):
    from nightshift.observe import _NullScope
    from nightshift import observe as observe_mod

    a = seed_widget(tmp_path / "alpha")
    b = seed_widget(tmp_path / "beta")
    (ns_home / "prior.json").write_text('{"liked": ["alpha"], "skip": []}', encoding="utf-8")
    settings = _settings(ns_home, [tmp_path], skip_meta=True, observe=True, loopscope_port=17994)
    scopes: list[object] = []
    real = observe_mod.start

    def wrap(**kwargs):
        scope = real(**kwargs)
        scopes.append(scope)
        return scope

    monkeypatch.setattr("nightshift.runner.observe_start", wrap)
    run_night(a, settings, explicit=True)
    run_night(b, settings, explicit=True)
    assert len(scopes) == 2
    assert not isinstance(scopes[1], _NullScope)
