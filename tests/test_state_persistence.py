from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from nightshift.bag import load_bag, mutate_bag, pid_alive, save_bag
from nightshift.status import StatusBoard, halt_requested, live_owner, request_halt


def test_status_updates_preserve_other_instance_progress_and_halt(ns_home):
    runner = StatusBoard(ns_home)
    deck = StatusBoard(ns_home)
    runner.reset(state="running", runner_pid=os.getpid(), repo="alpha", turn=1)
    deck.read()
    runner.update(turn=7, checks={"1": {"ok": True}})
    deck.update(halt_requested=True)
    runner.update(brain="critic", remaining_count=1)

    status = StatusBoard(ns_home).read()
    assert status.repo == "alpha"
    assert status.turn == 7
    assert status.checks == {"1": {"ok": True}}
    assert status.halt_requested is True
    assert status.remaining_count == 1


def test_status_reset_clears_previous_night_progress(ns_home):
    StatusBoard(ns_home).update(
        state="halted", repo="alpha", turn=8, checks={"1": {}},
        brief={"upgrades": [{"id": 1}]}, turns=[{"turn": 8}],
        halt_requested=True, error="previous failure", main_untouched=False,
    )
    StatusBoard(ns_home).reset(state="running", runner_pid=os.getpid(), repo="beta")

    status = StatusBoard(ns_home).read()
    assert status.repo == "beta"
    assert status.turn == 0
    assert status.checks == {}
    assert status.brief == {}
    assert status.turns == []
    assert status.halt_requested is False
    assert status.main_untouched is True
    assert status.error == ""


@pytest.mark.parametrize("document", ["[]", "null", '"broken"', "123", "{", "\udcff"])
def test_status_and_halt_tolerate_corrupt_documents(ns_home, document):
    raw = document.encode("utf-8", errors="surrogateescape")
    (ns_home / "status.json").write_bytes(raw)
    (ns_home / "halt.request").write_bytes(raw)
    assert StatusBoard(ns_home).read().state == "idle"
    assert halt_requested(ns_home, os.getpid()) is False
    request_halt(ns_home, os.getpid())
    assert halt_requested(ns_home, os.getpid()) is True
    assert halt_requested(ns_home, os.getpid()) is False


def test_status_defaults_malformed_fields(ns_home):
    (ns_home / "status.json").write_text(json.dumps({
        "state": "running", "runner_pid": -1, "turn": "nine",
        "checks": [], "turns": {}, "deadline": "tomorrow",
        "halt_requested": "false", "repo": {"wrong": "shape"},
    }))
    status = StatusBoard(ns_home).read()
    assert status.state == "running"
    assert status.runner_pid is None
    assert status.turn == 0
    assert status.checks == {}
    assert status.turns == []
    assert status.deadline == 0
    assert status.halt_requested is False
    assert status.repo == ""


@pytest.mark.parametrize("pid", [None, "bad", -1, 0, True, 10**100])
def test_invalid_process_owners_cannot_lock_or_signal_groups(pid):
    assert pid_alive(pid) is False
    assert live_owner(pid) is False


def test_status_drops_unsignable_runner_pid(ns_home):
    (ns_home / "status.json").write_text(json.dumps({
        "state": "running", "runner_pid": 10**100, "remaining_count": 2,
    }))
    status = StatusBoard(ns_home).read()
    assert status.state == "running"
    assert status.runner_pid is None
    assert status.remaining_count == 2


def test_bag_mutations_serialize_halt_with_runner_progress(ns_home):
    save_bag(ns_home, {
        "state": "running", "runner_pid": os.getpid(), "halt_bag": False,
        "targets": [{"name": "alpha", "state": "running", "turn": 0}],
    })
    started = threading.Event()
    attempted = threading.Event()

    def progress_update(bag):
        started.set()
        assert attempted.wait(timeout=5)
        bag["targets"][0].update(state="done", turn=9)

    def halt_update():
        assert started.wait(timeout=5)
        attempted.set()
        mutate_bag(ns_home, lambda bag: bag.update(halt_bag=True))

    with ThreadPoolExecutor(max_workers=2) as pool:
        progress = pool.submit(mutate_bag, ns_home, progress_update)
        halt = pool.submit(halt_update)
        progress.result(timeout=5)
        halt.result(timeout=5)

    bag = load_bag(ns_home)
    assert bag["halt_bag"] is True
    assert bag["targets"][0] == {"name": "alpha", "state": "done", "turn": 9}


@pytest.mark.parametrize("targets", [5, "bad", {}, [None, 12, {"state": "queued"}]])
def test_bag_load_filters_malformed_target_rows(ns_home, targets):
    (ns_home / "bag.json").write_text(json.dumps({"targets": targets}))
    expected = [{"state": "queued"}] if isinstance(targets, list) else []
    assert load_bag(ns_home)["targets"] == expected
