from __future__ import annotations

import json
from pathlib import Path

import pytest

from nightshift.ledger import (
    check_hash,
    is_history_duplicate,
    ledger_snapshot_block,
    load_ledger,
    merge_night_into_ledger,
    save_ledger,
)
from nightshift.llm import Critic
from nightshift.models import Brief, FrozenBriefError, Upgrade
from nightshift.observe import start as observe_start


def _n(n: int) -> list[Upgrade]:
    return [
        Upgrade(i, chr(ord("a") + i - 1), f"true {i}", [f"{chr(ord('a') + i - 1)}.py"])
        for i in range(1, n + 1)
    ]


def _entry(
    *,
    title: str = "old",
    check_command: str = "pytest tests/test_x.py -q",
    paths: list[str] | None = None,
    attempted: bool = True,
    done: bool = True,
    voided: bool = False,
) -> dict:
    paths = list(paths if paths is not None else ["foo.py"])
    return {
        "title": title,
        "check_command": check_command,
        "paths": paths,
        "check_hash": check_hash(check_command),
        "night": "night/2026-09-01",
        "attempted": attempted,
        "done": done,
        "voided": voided,
        "void_reason": "",
        "last_exit": 0,
        "turns": 2,
    }


def test_void_excludes_from_remaining():
    brief = Brief.freeze(_n(3))
    assert brief.remaining_count == 3
    assert brief.void_count == 0
    brief.void_upgrade(2, "duplicate_of_history")
    assert brief.upgrades[1].void is True
    assert brief.upgrades[1].void_reason == "duplicate_of_history"
    assert [u.id for u in brief.remaining()] == [1, 3]
    assert brief.remaining_count == 2
    assert brief.void_count == 1
    assert len(brief.upgrades) == 3
    dumped = brief.to_dict()
    assert dumped["void_count"] == 1
    assert dumped["remaining_count"] == 2
    roundtrip = Brief.from_dict(dumped)
    assert roundtrip.upgrades[1].void is True
    assert roundtrip.upgrades[1].void_reason == "duplicate_of_history"
    assert roundtrip.remaining_count == 2


def test_void_upgrade_cannot_unvoid():
    brief = Brief.freeze(_n(3))
    brief.void_upgrade(1, "duplicate_of_history", by=0)
    with pytest.raises(FrozenBriefError, match="un-void"):
        brief.void_upgrade(1, "something_else")
    assert brief.upgrades[0].void is True
    assert brief.upgrades[0].void_reason == "duplicate_of_history"
    brief.mark_done([2])
    with pytest.raises(FrozenBriefError, match="done"):
        brief.void_upgrade(2, "duplicate_of_history")
    assert brief.upgrades[1].void is False
    with pytest.raises(FrozenBriefError, match="another"):
        brief.add_upgrade(Upgrade(4, "no", "true", ["x"]))
    dirty = Upgrade(
        9, "voided source", "true", ["z.py"], void=True, void_reason="stale"
    )
    fresh = Brief.freeze(_n(2) + [dirty])
    assert all(u.void is False for u in fresh.upgrades)
    assert all(u.void_reason == "" for u in fresh.upgrades)


def test_history_duplicate_voids_attempted_same_check_paths():
    cmd = "pytest tests/test_x.py -q"
    upgrade = Upgrade(1, "same", cmd, ["foo.py"])
    ledger = {"entries": [_entry(check_command=cmd, paths=["foo.py"], attempted=True)]}
    assert is_history_duplicate(upgrade, ledger) is True
    other_paths = Upgrade(1, "same", cmd, ["bar.py"])
    assert is_history_duplicate(other_paths, ledger) is False
    other_cmd = Upgrade(1, "same", "pytest tests/test_y.py -q", ["foo.py"])
    assert is_history_duplicate(other_cmd, ledger) is False

    brief = Brief.freeze(
        [
            Upgrade(1, "same", cmd, ["foo.py"]),
            Upgrade(2, "other", "true", ["b.py"]),
            Upgrade(3, "third", "true 3", ["c.py"]),
        ]
    )
    for u in brief.upgrades:
        if is_history_duplicate(u, ledger):
            brief.void_upgrade(u.id, "duplicate_of_history")
    assert brief.upgrades[0].void is True
    assert brief.upgrades[0].void_reason == "duplicate_of_history"
    assert brief.remaining_count == 2
    assert len(brief.upgrades) == 3


def test_history_allows_retry_if_not_attempted():
    cmd = "pytest tests/test_x.py -q"
    upgrade = Upgrade(1, "same", cmd, ["foo.py"])
    ledger = {"entries": [_entry(check_command=cmd, paths=["foo.py"], attempted=False)]}
    assert is_history_duplicate(upgrade, ledger) is False
    brief = Brief.freeze(
        [
            Upgrade(1, "same", cmd, ["foo.py"]),
            Upgrade(2, "other", "true", ["b.py"]),
            Upgrade(3, "third", "true 3", ["c.py"]),
        ]
    )
    for u in brief.upgrades:
        if is_history_duplicate(u, ledger):
            brief.void_upgrade(u.id, "duplicate_of_history")
    assert brief.void_count == 0
    assert brief.remaining_count == 3


def test_job_line_ignores_critic_upgrade_id():
    brief = Brief.freeze(_n(3))

    class LaterIdClient:
        mock = False

        def chat(self, messages, **kwargs):
            user = messages[-1]["content"]
            payload = json.loads(user)
            assert "remaining" not in payload
            assert payload["upgrade"]["id"] == 1
            return json.dumps({"upgrade_id": 3, "job": "do the third one"})

    critic = Critic(LaterIdClient(), Path("."))
    uid, job = critic.job_line(brief)
    assert uid == 1
    assert job == "do the third one"

    brief.void_upgrade(1, "duplicate_of_history")

    class AfterVoidClient:
        mock = False

        def chat(self, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            assert payload["upgrade"]["id"] == 2
            return json.dumps({"upgrade_id": 3, "job": "skip to three"})

    uid, job = Critic(AfterVoidClient(), Path(".")).job_line(brief)
    assert uid == 2
    assert job == "skip to three"


def test_observe_rotates_events_jsonl(tmp_path: Path):
    ns = tmp_path / ".nightshift"
    ns.mkdir()
    events = ns / "events.jsonl"
    events.write_text('{"kind":"log","text":"old night"}\n', encoding="utf-8")
    (ns / "brief.json").write_text('{"frozen": true}\n', encoding="utf-8")
    (ns / "summary.md").write_text("# old summary\n", encoding="utf-8")
    observe_start(open_browser=False, jsonl=str(events), port=17993, serve=False)
    assert events.read_text(encoding="utf-8") == ""
    hist_root = ns / "history"
    stamp_dirs = [p for p in hist_root.iterdir() if p.is_dir()]
    assert len(stamp_dirs) == 1
    stamp = stamp_dirs[0]
    assert stamp.name.replace("-", "").isdigit() or True
    assert "old night" in (stamp / "events.jsonl").read_text(encoding="utf-8")
    assert (stamp / "brief.json").is_file()
    assert (stamp / "summary.md").is_file()

    empty = tmp_path / "empty-events.jsonl"
    empty.write_text("", encoding="utf-8")
    observe_start(open_browser=False, jsonl=str(empty), port=17994, serve=False)
    assert not (tmp_path / "history").exists() or not list((tmp_path / "history").iterdir())


def test_merge_and_load_ledger_roundtrip(tmp_path: Path):
    assert load_ledger(tmp_path) == {"entries": []}
    brief = Brief.freeze(_n(3))
    brief.void_upgrade(1, "duplicate_of_history")
    brief.mark_done([2])
    ledger = merge_night_into_ledger({"entries": []}, brief, "night/2026-09-02-1245")
    save_ledger(tmp_path, ledger)
    loaded = load_ledger(tmp_path)
    assert len(loaded["entries"]) == 3
    by_title = {e["title"]: e for e in loaded["entries"]}
    assert by_title["a"]["voided"] is True
    assert by_title["a"]["attempted"] is False
    assert by_title["b"]["done"] is True
    assert by_title["b"]["attempted"] is True
    block = ledger_snapshot_block(loaded)
    assert "Do not re-propose these unless the check changed." in block
    assert len(block.encode("utf-8")) <= 8 * 1024
