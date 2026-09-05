from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime
from pathlib import Path

import pytest

from nightshift.config import Settings
from nightshift.demo import seed_widget
from nightshift.forum import (
    FORUM_SCHEMA,
    MERGED_BY_GIT,
    MERGED_BY_OPERATOR,
    atomic_write_json,
    empty_forum,
    forum_enabled,
    ingest_forum,
    load_forum,
    mark_merged,
    mutate_forum,
    night_merged,
    render_forum_md,
    save_forum,
    strict_default_branch,
    upsert_item,
    upsert_night,
    with_home_lock,
)
from nightshift.gitops import current_branch, git
from nightshift.ledger import check_hash, item_id, night_id, pathset_hash, repo_id
from nightshift.models import SafetyError
from nightshift.runner import run_night

AINEKO = "Aineko · portfolio ledger · not a chat."


def _run_night_unpublished(repo, settings, monkeypatch):
    """A night with the forum off. Every run_night publishes since PR 2; the
    tests below exercise ingest's projection from ledgers alone."""
    monkeypatch.setenv("NIGHTSHIFT_FORUM", "0")
    try:
        return run_night(repo, settings, explicit=True)
    finally:
        monkeypatch.delenv("NIGHTSHIFT_FORUM", raising=False)


def _item(rid: str, night: str, title: str, cmd: str, paths: list[str], **over) -> dict:
    row = {
        "id": item_id(rid, check_hash(cmd), paths),
        "repo_id": rid,
        "repo_name": "widget",
        "night": night,
        "title": title,
        "check_command": cmd,
        "check_hash": check_hash(cmd),
        "paths": list(paths),
        "attempted": True,
        "done": True,
        "voided": False,
        "void_reason": "",
        "last_exit": 0,
        "turns": 1,
        "note": "",
        "lens": "",
    }
    row.update(over)
    return row


def _night(rid: str, night: str, item_ids: list[str], **over) -> dict:
    row = {
        "id": night_id(rid, night),
        "repo_id": rid,
        "repo_name": "widget",
        "repo_path": "/tmp/widget",
        "meta": False,
        "night": night,
        "branch": night,
        "started_at": "",
        "ended_at": "",
        "halt_reason": "remaining_zero",
        "base_ref": "main",
        "base_sha": "",
        "main_untouched": True,
        "merged": False,
        "landed": 1,
        "voided": 0,
        "remaining": 0,
        "error": "",
        "mock": True,
        "brief_size": 2,
        "lens_hint": "",
        "item_ids": list(item_ids),
    }
    row.update(over)
    return row


# --- identity ----------------------------------------------------------------


def test_identity_helpers(tmp_path):
    rid = repo_id(tmp_path)
    assert len(rid) == 12 and int(rid, 16) >= 0
    assert repo_id(tmp_path) == repo_id(Path(str(tmp_path)) / "." )
    assert pathset_hash(["b.py", "./a.py"]) == pathset_hash(["a.py", "b.py"])
    assert pathset_hash(["a.py"]) != pathset_hash(["b.py"])
    assert night_id(rid, "night/2026-09-03") == f"n-{rid}-night-2026-09-03"
    assert item_id(rid, "abc", ["a.py"]) == f"i-{rid}-abc-{pathset_hash(['a.py'])}"


def test_forum_enabled_env(monkeypatch):
    monkeypatch.delenv("NIGHTSHIFT_FORUM", raising=False)
    assert forum_enabled() is True
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("NIGHTSHIFT_FORUM", off)
        assert forum_enabled() is False
    monkeypatch.setenv("NIGHTSHIFT_FORUM", "1")
    assert forum_enabled() is True


# --- load / save -------------------------------------------------------------


def test_empty_document_and_missing_file(tmp_path):
    expected = {
        "schema": 1,
        "updated_at": "",
        "nights": [],
        "items": [],
        "reuse_events": [],
        "errors": [],
    }
    assert empty_forum() == expected
    assert empty_forum() is not empty_forum()
    assert load_forum(tmp_path) == expected
    assert FORUM_SCHEMA == 1


def test_load_tolerates_corrupt_and_odd_files(tmp_path):
    path = tmp_path / "forum.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_forum(tmp_path) == empty_forum()
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_forum(tmp_path) == empty_forum()
    path.write_text(json.dumps({"schema": 0, "nights": []}), encoding="utf-8")
    assert load_forum(tmp_path) == empty_forum()
    path.write_text(json.dumps({"nights": "nope", "items": [1, {"id": "x"}]}), encoding="utf-8")
    loaded = load_forum(tmp_path)
    assert loaded["schema"] == 1
    assert loaded["nights"] == []
    assert loaded["items"] == [{"id": "x"}]
    assert loaded["reuse_events"] == [] and loaded["errors"] == []


def test_unknown_keys_survive_roundtrip(ns_home):
    doc = {
        "schema": 3,
        "updated_at": "",
        "nights": [{"repo_id": "a", "night": "night/x", "future_field": {"k": 1}}],
        "items": [{"repo_id": "a", "check_hash": "h", "paths": ["a.py"], "extra": [1, 2]}],
        "reuse_events": [],
        "errors": [],
        "custom_top": "kept",
    }
    (ns_home / "forum.json").write_text(json.dumps(doc), encoding="utf-8")
    first = load_forum(ns_home)
    save_forum(ns_home, first)
    again = load_forum(ns_home)
    assert again["custom_top"] == "kept"
    assert again["schema"] == 3
    assert again["nights"][0]["future_field"] == {"k": 1}
    assert again["items"][0]["extra"] == [1, 2]
    assert again["updated_at"]
    assert not (ns_home / "forum.json.tmp").exists()


def test_load_normalises_corrupt_row_list_fields(ns_home):
    doc = empty_forum()
    doc["items"] = [
        {"repo_id": "a", "check_hash": "h", "paths": 5, "done": True},
        {"repo_id": "a", "check_hash": "g", "paths": ["a.py", 5, None, "b.py"]},
        {"id": "no-paths"},
    ]
    doc["nights"] = [
        {"repo_id": "a", "night": "night/2026-01-01", "branch": "night/2026-01-01", "item_ids": 5},
        {"repo_id": "a", "night": "night/2026-01-02", "item_ids": "x"},
        {"repo_id": "a", "night": "night/2026-01-03"},
    ]
    (ns_home / "forum.json").write_text(json.dumps(doc), encoding="utf-8")
    loaded = load_forum(ns_home)
    assert [i.get("paths") for i in loaded["items"]] == [[], ["a.py", "b.py"], None]
    assert [n.get("item_ids") for n in loaded["nights"]] == [[], [], None]
    # a corrupt stored row ahead of the match no longer poisons the upsert loop
    new = _item("a", "night/2026-01-04", "t", "true", ["c.py"])
    assert upsert_item(loaded, new) is new
    assert len(loaded["items"]) == 4
    save_forum(ns_home, loaded)
    md = (ns_home / "forum.md").read_text(encoding="utf-8")
    assert md.count("- 2026-01-0") == 3
    # same for a hand-built document that never went through load_forum
    forum = empty_forum()
    forum["items"] = [{"repo_id": "a", "check_hash": "h", "paths": 5, "done": True}]
    upsert_item(forum, _item("a", "night/x", "t", "true", ["c.py"]))
    assert len(forum["items"]) == 2


def test_save_forum_render_failure_leaves_both_files_untouched(ns_home, monkeypatch):
    save_forum(ns_home, empty_forum())
    json_before = (ns_home / "forum.json").read_bytes()
    md_before = (ns_home / "forum.md").read_bytes()

    def boom(forum, **kwargs):
        raise RuntimeError("render broke")

    monkeypatch.setattr("nightshift.forum.render_forum_md", boom)
    doc = empty_forum()
    doc["errors"] = [{"at": "t", "repo_name": "x", "error": "e"}]
    with pytest.raises(RuntimeError, match="render broke"):
        save_forum(ns_home, doc)
    assert (ns_home / "forum.json").read_bytes() == json_before
    assert (ns_home / "forum.md").read_bytes() == md_before
    assert sorted(p.name for p in ns_home.iterdir()) == ["forum.json", "forum.lock", "forum.md"]


def test_atomic_write_json_preserves_document_and_cleans_up_when_replace_fails(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target = home / "forum.json"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("nightshift.forum.os.replace", boom)
    with pytest.raises(OSError):
        atomic_write_json(target, {"schema": 1})
    assert list(home.iterdir()) == []

    monkeypatch.undo()
    atomic_write_json(target, {"schema": 1, "v": "old"})
    monkeypatch.setattr("nightshift.forum.os.replace", boom)
    with pytest.raises(OSError):
        atomic_write_json(target, {"schema": 1, "v": "new"})
    assert json.loads(target.read_text()) == {"schema": 1, "v": "old"}
    assert sorted(p.name for p in home.iterdir()) == ["forum.json"]


def test_with_home_lock_releases_on_exception(tmp_path):
    def raiser():
        raise RuntimeError("inside")

    with pytest.raises(RuntimeError, match="inside"):
        with_home_lock(tmp_path, "forum", raiser)
    assert (tmp_path / "forum.lock").is_file()

    result: list[str] = []
    t = threading.Thread(target=lambda: result.append(with_home_lock(tmp_path, "forum", lambda: "ok")))
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()
    assert result == ["ok"]


def test_with_home_lock_serialises_holders(tmp_path):
    held = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def holder():
        def _inside():
            held.set()
            release.wait(timeout=10)
            order.append("a")

        with_home_lock(tmp_path, "bag", _inside)

    a = threading.Thread(target=holder)
    a.start()
    assert held.wait(timeout=5)
    b = threading.Thread(target=lambda: with_home_lock(tmp_path, "bag", lambda: order.append("b")))
    b.start()
    b.join(timeout=0.3)
    assert b.is_alive(), "second holder must block while the first holds the lock"
    release.set()
    a.join(timeout=5)
    b.join(timeout=5)
    assert order == ["a", "b"]


def test_save_forum_writes_md_with_aineko(ns_home):
    path = save_forum(ns_home, empty_forum())
    assert path == ns_home / "forum.json"
    md = (ns_home / "forum.md").read_text(encoding="utf-8")
    assert md.startswith("# Nightshift forum\n" + AINEKO + "\n")
    assert "## Tonight's bag" not in md
    assert "## Nights\n- (none yet)" in md
    assert "## Reuse\n- (none yet)" in md
    assert "## Errors\n- (none)" in md
    assert "## Land\n- (none)" in md
    assert load_forum(ns_home)["updated_at"]
    assert not (ns_home / "forum.md.tmp").exists()


# --- upsert rules ------------------------------------------------------------


def test_item_upsert_keeps_done_row():
    forum = empty_forum()
    rid = "c0ffee12ab34"
    cmd = "pytest tests/test_x.py -q"
    landed = _item(rid, "night/2026-09-01", "shlex catch", cmd, ["host.py"], note="")
    upsert_item(forum, landed)
    later_void = _item(
        rid, "night/2026-09-02", "shlex catch again", cmd, ["./host.py"],
        done=False, voided=True, void_reason="duplicate_of_history", note="from void", turns=3,
    )
    kept = upsert_item(forum, later_void)
    assert len(forum["items"]) == 1
    assert kept["done"] is True and kept["voided"] is False
    assert kept["night"] == "night/2026-09-01"
    assert kept["title"] == "shlex catch"
    assert kept["note"] == "from void"
    assert kept["turns"] == 3
    upsert_item(forum, dict(later_void, note="second void", turns=2))
    assert forum["items"][0]["note"] == "from void"
    assert forum["items"][0]["turns"] == 3
    # a non-done stored row is replaced by a later done row
    other = _item(rid, "night/2026-09-01", "open one", "true 2", ["b.py"], done=False, attempted=False)
    upsert_item(forum, other)
    upsert_item(forum, dict(other, done=True, attempted=True, night="night/2026-09-03"))
    assert len(forum["items"]) == 2
    assert forum["items"][1]["done"] is True and forum["items"][1]["night"] == "night/2026-09-03"
    # a different repo with the same check+paths is a different row
    upsert_item(forum, _item("d00d00d00d00", "night/2026-09-04", "shlex catch", cmd, ["host.py"]))
    assert len(forum["items"]) == 3
    # lens: only a live publish knows it. An ingest / re-projection of the same
    # done row (lens "") never blanks a stored lens; a non-empty one replaces it.
    lensed = _item(rid, "night/2026-09-05", "lensed", "true 3", ["c.py"], lens="de")
    upsert_item(forum, lensed)
    kept = upsert_item(forum, dict(lensed, lens=""))
    assert kept["lens"] == "de" and len(forum["items"]) == 4
    assert upsert_item(forum, dict(lensed, lens="oe"))["lens"] == "oe"
    # a later same-key void never relabels the kept done row with its own lens
    upsert_item(forum, dict(lensed, done=False, voided=True, night="night/2026-09-06", lens="de"))
    assert forum["items"][3]["lens"] == "oe" and forum["items"][3]["done"] is True


# --- ingest ------------------------------------------------------------------


def test_ingest_mock_night_merged_only_by_git_evidence(
    fixture_repo, mock_settings, ns_home, monkeypatch
):
    report = _run_night_unpublished(fixture_repo, mock_settings, monkeypatch)
    rid = repo_id(fixture_repo)
    clone_ledger = fixture_repo / ".nightshift" / "ledger.json"
    shard = ns_home / "ledger" / f"{rid}.json"
    clone_before, shard_before = clone_ledger.read_bytes(), shard.read_bytes()
    stats: dict[str, int] = {}
    forum = ingest_forum(ns_home, [fixture_repo], stats=stats)
    assert stats == {"repos": 1, "nights": 1, "items": 2, "orphans": 0}
    # ingest never rewrites the clone ledger or the home shard
    assert clone_ledger.read_bytes() == clone_before
    assert shard.read_bytes() == shard_before
    assert len(forum["nights"]) == 1 and len(forum["items"]) == 2
    night = forum["nights"][0]
    assert night["halt_reason"] == "ingested"
    assert night["night"] == report.branch == night["branch"]
    assert night["repo_id"] == rid and night["id"] == night_id(rid, report.branch)
    assert night["repo_path"] == str(fixture_repo) and night["repo_name"] == fixture_repo.name
    assert night["meta"] is False and night["mock"] is False
    assert night["landed"] == 2 and night["voided"] == 0 and night["remaining"] == 0
    assert night["merged"] is False
    assert night["item_ids"] == [i["id"] for i in forum["items"]]
    for item in forum["items"]:
        assert item["id"].startswith(f"i-{rid}-")
        assert item["done"] is True and item["attempted"] is True
        assert item["paths"] == ["widget.py"]
        assert item["night"] == report.branch
    assert forum["reuse_events"] == []
    assert (ns_home / "forum.md").read_text(encoding="utf-8").count(AINEKO) == 1
    # HEAD on main is not merge evidence: the ledger lives on the night branch,
    # so these rows now come from the home shard, which is never merge proof
    git(fixture_repo, "checkout", "main")
    assert not clone_ledger.exists()
    forum = ingest_forum(ns_home, [fixture_repo], stats=stats)
    assert stats["nights"] == 1 and stats["items"] == 2
    assert forum["nights"][0]["merged"] is False
    assert "git merge --no-ff " + report.branch in (ns_home / "forum.md").read_text()
    # a real merge into main flips it
    git(fixture_repo, "merge", "--no-ff", "-m", "land the night", report.branch)
    forum = ingest_forum(ns_home, [fixture_repo], stats=stats)
    assert stats == {"repos": 1, "nights": 1, "items": 2, "orphans": 0}
    assert len(forum["nights"]) == 1 and len(forum["items"]) == 2
    assert forum["nights"][0]["merged"] is True
    assert "git merge --no-ff " + report.branch not in (ns_home / "forum.md").read_text()


def test_ingest_counts_orphan_shards_without_guessing(fixture_repo, ns_home):
    shard_dir = ns_home / "ledger"
    shard_dir.mkdir(parents=True)
    (shard_dir / "deadbeefdead.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "title": "ghost",
                        "check_command": "pytest -q",
                        "paths": ["x.py"],
                        "check_hash": check_hash("pytest -q"),
                        "night": "night/2026-01-01",
                        "attempted": True,
                        "done": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    stats: dict[str, int] = {}
    forum = ingest_forum(ns_home, [fixture_repo], stats=stats)
    assert stats == {"repos": 1, "nights": 0, "items": 0, "orphans": 1}
    assert forum["nights"] == [] and forum["items"] == []
    assert (shard_dir / "deadbeefdead.json").is_file()


def test_ingest_filters_blocked_paths_and_clips_notes(fixture_repo, ns_home):
    ledger_path = fixture_repo / ".nightshift" / "ledger.json"
    ledger_path.parent.mkdir(exist_ok=True)
    cmd = "pytest tests/test_widget.py -q"
    ledger_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "title": "touch env",
                        "check_command": cmd,
                        "paths": ["widget.py", ".env", "./widget.py", ".git/config"],
                        "check_hash": check_hash(cmd),
                        "night": "night/2026-02-02",
                        "attempted": True,
                        "done": False,
                        "voided": True,
                        "void_reason": "failed_before:night/2026-02-01",
                        "last_exit": "2",
                        "turns": 4,
                        "note": "x" * 900,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    forum = ingest_forum(ns_home, [fixture_repo])
    item = forum["items"][0]
    assert item["paths"] == ["widget.py"]
    assert item["id"] == item_id(repo_id(fixture_repo), check_hash(cmd), ["widget.py"])
    assert len(item["note"]) == 500
    assert item["last_exit"] == 2 and item["turns"] == 4
    assert item["attempted"] is True and item["done"] is False and item["voided"] is True
    night = forum["nights"][0]
    assert (night["landed"], night["voided"], night["remaining"]) == (0, 1, 0)
    assert "[void failed_before:night/2026-02-01] touch env" in (ns_home / "forum.md").read_text()


def test_ingest_and_mark_merged_survive_corrupt_forum_rows(fixture_repo, ns_home):
    ledger_path = fixture_repo / ".nightshift" / "ledger.json"
    ledger_path.parent.mkdir(exist_ok=True)
    cmd = "pytest -q"
    ledger_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "title": "t",
                        "check_command": cmd,
                        "paths": ["widget.py"],
                        "check_hash": check_hash(cmd),
                        "night": "night/2026-02-02",
                        "attempted": True,
                        "done": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    ingest_forum(ns_home, [fixture_repo])
    doc = load_forum(ns_home)
    doc["items"].insert(0, {"repo_id": "zzz", "check_hash": "h", "paths": 5, "done": True})
    doc["nights"].append(
        {"repo_id": "q", "night": "night/2026-01-01", "branch": "night/2026-01-01", "item_ids": 5}
    )
    (ns_home / "forum.json").write_text(json.dumps(doc), encoding="utf-8")
    stats: dict[str, int] = {}
    forum = ingest_forum(ns_home, [fixture_repo], stats=stats)
    assert stats == {"repos": 1, "nights": 1, "items": 1, "orphans": 0}
    assert len(forum["items"]) == 2 and len(forum["nights"]) == 2
    assert forum["items"][0]["paths"] == [] and forum["nights"][1]["item_ids"] == []
    forum = mark_merged(ns_home, fixture_repo, night="night/2026-02-02")
    assert [n.get("merged") for n in forum["nights"]] == [True, None]
    md = (ns_home / "forum.md").read_text(encoding="utf-8")
    assert f"Updated {forum['updated_at']}" in md
    assert "- 2026-01-01  q  night/2026-01-01" in md


# --- mark-merged -------------------------------------------------------------


def test_mark_merged_explicit_night(fixture_repo, mock_settings, ns_home):
    report = run_night(fixture_repo, mock_settings, explicit=True)
    ingest_forum(ns_home, [fixture_repo])
    with pytest.raises(SafetyError):
        mark_merged(ns_home, fixture_repo, night="night/1999-01-01")
    forum = mark_merged(ns_home, fixture_repo, night=report.branch)
    assert forum["nights"][0]["merged"] is True
    assert load_forum(ns_home)["nights"][0]["merged"] is True
    # ingest again: git says unmerged, but the operator mark is sticky
    forum = ingest_forum(ns_home, [fixture_repo])
    assert forum["nights"][0]["merged"] is True


def test_mark_merged_without_night_stamps_only_the_most_recent(ns_home, tmp_path):
    repo = tmp_path / "widget"
    repo.mkdir()
    rid = repo_id(repo)
    old = _item(rid, "night/2026-09-01", "a", "true a", ["a.py"])
    new = _item(rid, "night/2026-09-02", "b", "true b", ["b.py"])
    forum = empty_forum()
    forum["items"] = [old, new]
    forum["nights"] = [
        _night(rid, "night/2026-09-01", [old["id"]], ended_at="2026-09-01T05:00:00+00:00"),
        _night(rid, "night/2026-09-02", [new["id"]], ended_at="2026-09-02T05:00:00+00:00"),
    ]
    save_forum(ns_home, forum)
    out = mark_merged(ns_home, repo)
    by_night = {n["night"]: n["merged"] for n in out["nights"]}
    assert by_night == {"night/2026-09-01": False, "night/2026-09-02": True}
    out = mark_merged(ns_home, repo)
    assert {n["night"]: n["merged"] for n in out["nights"]} == {
        "night/2026-09-01": True,
        "night/2026-09-02": True,
    }
    with pytest.raises(SafetyError, match="no unmerged"):
        mark_merged(ns_home, repo)
    with pytest.raises(SafetyError, match="no forum nights"):
        mark_merged(ns_home, tmp_path / "unknown")


def test_mark_merged_ignores_nights_without_done_items(ns_home, tmp_path):
    repo = tmp_path / "widget"
    repo.mkdir()
    rid = repo_id(repo)
    done = _item(rid, "night/2026-09-01", "a", "true a", ["a.py"])
    void = _item(rid, "night/2026-09-05", "b", "true b", ["b.py"], done=False, voided=True)
    forum = empty_forum()
    forum["items"] = [done, void]
    forum["nights"] = [
        _night(rid, "night/2026-09-01", [done["id"]]),
        _night(rid, "night/2026-09-05", [void["id"]], landed=0, voided=1),
    ]
    save_forum(ns_home, forum)
    out = mark_merged(ns_home, repo)
    assert {n["night"]: n["merged"] for n in out["nights"]} == {
        "night/2026-09-01": True,
        "night/2026-09-05": False,
    }


# --- forum.md ----------------------------------------------------------------


def test_render_forum_md_bag_section(ns_home):
    rid = "c0ffee12ab34"
    item = _item(rid, "night/2026-09-03", "quote host checks with shlex", "pytest -q", ["host.py"])
    forum = empty_forum()
    forum["items"] = [item]
    forum["nights"] = [_night(rid, "night/2026-09-03", [item["id"]], repo_name="nightshift")]
    text = render_forum_md(forum)
    assert "## Tonight's bag" not in text
    assert "- 2026-09-03  nightshift  night/2026-09-03  remaining_zero  landed 1 / void 0 / open 0" in text
    assert "  - [done] quote host checks with shlex  `pytest -q`" in text
    assert "- nightshift: `git checkout main && git merge --no-ff night/2026-09-03`" in text
    assert "- nightshift: `git branch -D night/2026-09-03`" in text
    assert render_forum_md(forum, bag={"state": "", "targets": []}) == text
    assert "## Tonight's bag" not in render_forum_md(forum, home=ns_home)

    bag = {
        "state": "running",
        "targets": [
            {"repo_id": rid, "name": "nightshift", "state": "done", "branch": "night/2026-09-03", "halt_reason": "remaining_zero"},
            {"repo_id": "aaaa", "name": "loopscope", "state": "skipped", "error": "dirty tree"},
        ],
    }
    with_bag = render_forum_md(forum, bag=bag)
    assert "## Tonight's bag" in with_bag
    assert "- nightshift  night/2026-09-03  remaining_zero  1 landed  0 void" in with_bag
    assert "- loopscope  skipped: dirty tree" in with_bag
    (ns_home / "bag.json").write_text(json.dumps(bag), encoding="utf-8")
    assert "## Tonight's bag" in render_forum_md(forum, home=ns_home)
    save_forum(ns_home, forum)
    md = (ns_home / "forum.md").read_text(encoding="utf-8")
    assert "## Tonight's bag" in md and "- loopscope  skipped: dirty tree" in md
    (ns_home / "bag.json").write_text("{corrupt", encoding="utf-8")
    assert "## Tonight's bag" not in render_forum_md(forum, home=ns_home)


def test_render_forum_md_tolerates_scalar_list_fields(ns_home):
    forum = empty_forum()
    night = _night("a", "night/2026-01-01", [])
    night["item_ids"] = 5
    forum["nights"] = [night]
    forum["reuse_events"] = 7
    forum["errors"] = None
    text = render_forum_md(forum, bag={"state": "running", "targets": 5})
    assert "## Tonight's bag\n- (no targets; bag running)" in text
    assert "- 2026-01-01  widget  night/2026-01-01  remaining_zero" in text
    assert "## Reuse\n- (none yet)" in text and "## Errors\n- (none)" in text
    (ns_home / "bag.json").write_text(json.dumps({"state": "running", "targets": 5}), encoding="utf-8")
    save_forum(ns_home, empty_forum())
    assert "- (no targets; bag running)" in (ns_home / "forum.md").read_text(encoding="utf-8")


def test_render_forum_md_never_carries_long_errors(ns_home):
    forum = empty_forum()
    forum["errors"] = [{"at": "t", "repo_name": "x", "error": "e" * 5000}]
    text = render_forum_md(forum)
    assert "e" * 201 not in text
    assert text.endswith("\n")


# --- merge evidence ----------------------------------------------------------


def _dated_settings(ns_home, day: int) -> Settings:
    return Settings(
        mock=True,
        observe=False,
        home=ns_home,
        max_turns=12,
        stall_after=12,
        check_timeout=30,
        push=False,
        now_fn=lambda: datetime(2026, 9, day, 1, 0),
    )


def test_night_merged_never_treats_head_as_default(
    fixture_repo, mock_settings, ns_home, monkeypatch
):
    git(fixture_repo, "branch", "-m", "main", "trunk")
    report = _run_night_unpublished(fixture_repo, mock_settings, monkeypatch)
    assert current_branch(fixture_repo) == report.branch
    # no main/master/origin: no trunk to prove against, so no evidence, not HEAD
    assert strict_default_branch(fixture_repo) == ""
    assert night_merged(fixture_repo, report.branch, []) is False
    forum = ingest_forum(ns_home, [fixture_repo])
    night = forum["nights"][0]
    assert night["merged"] is False and night["merged_by"] == ""
    assert night["base_ref"] == ""
    md = (ns_home / "forum.md").read_text(encoding="utf-8")
    assert f"git checkout {report.branch}" not in md
    assert f"git checkout <base> && git merge --no-ff {report.branch}" in md
    assert "default branch unknown" in md
    assert f"git branch -D {report.branch}" in md
    # HEAD on trunk, still no evidence
    git(fixture_repo, "checkout", "trunk")
    assert ingest_forum(ns_home, [fixture_repo])["nights"][0]["merged"] is False
    # a branch that merely contains the night is not the default either
    git(fixture_repo, "checkout", "-b", "feature-x", report.branch)
    assert night_merged(fixture_repo, report.branch, []) is False
    git(fixture_repo, "checkout", "trunk")
    # an explicit origin/HEAD is a real trunk (never HEAD): unmerged until merged
    git(fixture_repo, "update-ref", "refs/remotes/origin/trunk", "trunk")
    git(fixture_repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert strict_default_branch(fixture_repo) == "trunk"
    forum = ingest_forum(ns_home, [fixture_repo])
    assert forum["nights"][0]["merged"] is False
    assert forum["nights"][0]["base_ref"] == "trunk"
    md = (ns_home / "forum.md").read_text(encoding="utf-8")
    assert f"git checkout trunk && git merge --no-ff {report.branch}" in md
    git(fixture_repo, "merge", "--no-ff", "-m", "land", report.branch)
    forum = ingest_forum(ns_home, [fixture_repo])
    assert forum["nights"][0]["merged"] is True
    assert forum["nights"][0]["merged_by"] == MERGED_BY_GIT


def test_ingest_dropped_night_never_merged_via_home_shard(fixture_repo, ns_home):
    r1 = run_night(fixture_repo, _dated_settings(ns_home, 3), explicit=True)
    git(fixture_repo, "checkout", "main")
    git(fixture_repo, "branch", "-D", r1.branch)  # night 1 dropped: its diff never lands
    r2 = run_night(fixture_repo, _dated_settings(ns_home, 4), explicit=True)
    assert r2.branch != r1.branch
    assert all(u.void for u in r2.brief.upgrades)
    git(fixture_repo, "checkout", "main")
    git(fixture_repo, "merge", "--no-ff", "-m", "land night 2", r2.branch)
    # the leak: main's ledger now carries night 1's done rows (via the home shard)
    shown = json.loads(git(fixture_repo, "show", "main:.nightshift/ledger.json").stdout)
    assert {e["night"] for e in shown["entries"]} == {r1.branch}
    assert all(e["done"] for e in shown["entries"])
    assert git(fixture_repo, "log", "--format=%s", "main", "--", "widget.py").stdout.count("\n") == 1
    done_rows = [dict(e) for e in shown["entries"]]
    assert night_merged(fixture_repo, r1.branch, done_rows) is False
    assert night_merged(fixture_repo, r1.branch, []) is False
    forum = ingest_forum(ns_home, [fixture_repo])
    by_night = {n["night"]: n for n in forum["nights"]}
    assert by_night[r1.branch]["merged"] is False
    assert f"git merge --no-ff {r1.branch}" in (ns_home / "forum.md").read_text(encoding="utf-8")


def test_ingest_merged_night_survives_branch_delete_and_stale_stamp_clears(
    fixture_repo, mock_settings, ns_home
):
    report = run_night(fixture_repo, mock_settings, explicit=True)
    git(fixture_repo, "checkout", "main")
    git(fixture_repo, "merge", "--no-ff", "-m", "land", report.branch)
    git(fixture_repo, "branch", "-D", report.branch)
    # rule (1): the night's own ledger commit is on main, branch gone
    forum = ingest_forum(ns_home, [fixture_repo])
    assert forum["nights"][0]["merged"] is True
    assert forum["nights"][0]["merged_by"] == MERGED_BY_GIT
    # un-merge: git evidence is gone, so the git stamp is recomputed to False
    git(fixture_repo, "reset", "--hard", report.main_sha)
    forum = ingest_forum(ns_home, [fixture_repo])
    assert forum["nights"][0]["merged"] is False
    assert forum["nights"][0]["merged_by"] == ""
    # an operator mark is sticky across ingests
    forum = mark_merged(ns_home, fixture_repo, night=report.branch)
    assert forum["nights"][0]["merged_by"] == MERGED_BY_OPERATOR
    forum = ingest_forum(ns_home, [fixture_repo])
    assert forum["nights"][0]["merged"] is True
    assert forum["nights"][0]["merged_by"] == MERGED_BY_OPERATOR


def test_operator_mark_survives_git_evidence_appearing_then_vanishing(
    fixture_repo, mock_settings, ns_home
):
    report = run_night(fixture_repo, mock_settings, explicit=True)
    ingest_forum(ns_home, [fixture_repo])
    forum = mark_merged(ns_home, fixture_repo, night=report.branch)
    assert forum["nights"][0]["merged_by"] == MERGED_BY_OPERATOR
    # git evidence appears: the operator stamp is never downgraded to "git"
    git(fixture_repo, "checkout", "main")
    git(fixture_repo, "merge", "--no-ff", "-m", "land", report.branch)
    forum = ingest_forum(ns_home, [fixture_repo])
    assert forum["nights"][0]["merged"] is True
    assert forum["nights"][0]["merged_by"] == MERGED_BY_OPERATOR
    # git evidence vanishes: rule (3) alone still keeps the night merged
    git(fixture_repo, "reset", "--hard", report.main_sha)
    git(fixture_repo, "branch", "-D", report.branch)
    assert night_merged(fixture_repo, report.branch, []) is False
    forum = ingest_forum(ns_home, [fixture_repo])
    assert forum["nights"][0]["merged"] is True
    assert forum["nights"][0]["merged_by"] == MERGED_BY_OPERATOR
    assert load_forum(ns_home)["nights"][0]["merged_by"] == MERGED_BY_OPERATOR


def test_ingest_reverted_merge_is_not_merged(fixture_repo, mock_settings, ns_home):
    report = run_night(fixture_repo, mock_settings, explicit=True)
    git(fixture_repo, "checkout", "main")
    git(fixture_repo, "merge", "--no-ff", "-m", "land", report.branch)
    assert ingest_forum(ns_home, [fixture_repo])["nights"][0]["merged"] is True
    git(fixture_repo, "revert", "-m", "1", "--no-edit", "HEAD")
    git(fixture_repo, "branch", "-D", report.branch)
    # the merge commit is still in main's history (provenance), but the file on
    # the default branch no longer holds the night's done rows (literal rule 1)
    assert git(fixture_repo, "show", "main:.nightshift/ledger.json", check=False).returncode != 0
    assert (fixture_repo / "widget.py").read_bytes() == git(
        fixture_repo, "show", f"{report.main_sha}:widget.py"
    ).stdout.encode()
    assert night_merged(fixture_repo, report.branch, []) is False
    forum = ingest_forum(ns_home, [fixture_repo])
    assert forum["nights"][0]["merged"] is False
    assert forum["nights"][0]["merged_by"] == ""


def test_ingest_hand_copied_ledger_without_brief_is_not_merged(fixture_repo, mock_settings, ns_home):
    # literal rule (1) alone would say True; provenance (a commit on main whose
    # brief.json names the night) is still required — that landing is mark-merged
    report = run_night(fixture_repo, mock_settings, explicit=True)
    git(fixture_repo, "checkout", "main")
    shown = git(fixture_repo, "show", f"{report.branch}:.nightshift/ledger.json").stdout
    (fixture_repo / ".nightshift").mkdir(exist_ok=True)
    (fixture_repo / ".nightshift" / "ledger.json").write_text(shown, encoding="utf-8")
    git(fixture_repo, "add", "-f", ".nightshift/ledger.json")
    git(fixture_repo, "commit", "-m", "hand-landed ledger")
    git(fixture_repo, "branch", "-D", report.branch)
    on_main = json.loads(git(fixture_repo, "show", "main:.nightshift/ledger.json").stdout)
    assert any(e.get("done") and e.get("night") == report.branch for e in on_main["entries"])
    assert git(fixture_repo, "show", "main:.nightshift/brief.json", check=False).returncode != 0
    assert night_merged(fixture_repo, report.branch, []) is False
    assert ingest_forum(ns_home, [fixture_repo])["nights"][0]["merged"] is False


def test_ingest_skips_gone_population_path(
    fixture_repo, mock_settings, ns_home, tmp_path, monkeypatch
):
    gone = seed_widget(tmp_path / "gone-widget")
    _run_night_unpublished(gone, mock_settings, monkeypatch)
    report = _run_night_unpublished(fixture_repo, mock_settings, monkeypatch)
    gone_rid = repo_id(gone)
    assert (ns_home / "ledger" / f"{gone_rid}.json").is_file()
    shutil.rmtree(gone)
    assert night_merged(gone, "night/2026-09-03", []) is False
    stats: dict[str, int] = {}
    forum = ingest_forum(ns_home, [gone, fixture_repo], stats=stats)
    # the gone clone is skipped, not projected; its shard is an orphan
    assert stats == {"repos": 1, "nights": 1, "items": 2, "orphans": 1}
    assert [n["repo_id"] for n in forum["nights"]] == [repo_id(fixture_repo)]
    assert forum["nights"][0]["night"] == report.branch
    assert all(i["repo_id"] != gone_rid for i in forum["items"])
    assert (ns_home / "forum.json").is_file()
    assert (ns_home / "ledger" / f"{gone_rid}.json").is_file()
    # a directory that is not a git work tree is skipped the same way
    plain = tmp_path / "plain"
    plain.mkdir()
    forum = ingest_forum(ns_home, [plain, fixture_repo], stats=stats)
    assert stats == {"repos": 1, "nights": 1, "items": 2, "orphans": 1}
    assert len(forum["nights"]) == 1


def test_ingest_is_additive_on_a_published_night(fixture_repo, mock_settings, ns_home):
    report = run_night(fixture_repo, mock_settings, explicit=True)
    rid = repo_id(fixture_repo)
    published = _night(
        rid,
        report.branch,
        ["i-published"],
        repo_name=fixture_repo.name,
        repo_path=str(fixture_repo),
        started_at="2026-09-03T02:00:00+00:00",
        ended_at="2026-09-03T04:10:00+00:00",
        halt_reason="remaining_zero",
        base_sha="abc123",
        mock=True,
        lens_hint="oe",
        landed=2,
        error="",
    )
    forum = empty_forum()
    upsert_night(forum, dict(published))
    save_forum(ns_home, forum)
    keep = (
        "started_at", "ended_at", "halt_reason", "base_ref", "base_sha", "mock",
        "lens_hint", "landed", "voided", "remaining", "brief_size", "item_ids", "error",
    )
    forum = ingest_forum(ns_home, [fixture_repo])
    assert len(forum["nights"]) == 1 and len(forum["items"]) == 2
    night = forum["nights"][0]
    assert {k: night[k] for k in keep} == {k: published[k] for k in keep}
    assert night["merged"] is False
    git(fixture_repo, "checkout", "main")
    git(fixture_repo, "merge", "--no-ff", "-m", "land", report.branch)
    night = ingest_forum(ns_home, [fixture_repo])["nights"][0]
    assert night["merged"] is True and night["merged_by"] == MERGED_BY_GIT
    assert {k: night[k] for k in keep} == {k: published[k] for k in keep}


def test_mutate_forum_serialises_read_modify_write(ns_home, tmp_path):
    ra, rb = tmp_path / "A", tmp_path / "B"
    ra.mkdir()
    rb.mkdir()
    forum = empty_forum()
    for repo in (ra, rb):
        rid = repo_id(repo)
        item = _item(rid, "night/2026-09-01", "a", "true a", ["a.py"], repo_name=repo.name)
        forum["items"].append(item)
        forum["nights"].append(_night(rid, "night/2026-09-01", [item["id"]], repo_name=repo.name))
    save_forum(ns_home, forum)
    held = threading.Event()
    release = threading.Event()

    def other_writer():
        def _inside():
            held.set()
            release.wait(timeout=10)
            doc = load_forum(ns_home)
            for night in doc["nights"]:
                if night["repo_name"] == "A":
                    night["merged"] = True
            atomic_write_json(ns_home / "forum.json", doc)

        with_home_lock(ns_home, "forum", _inside)

    a = threading.Thread(target=other_writer)
    a.start()
    assert held.wait(timeout=5)
    b = threading.Thread(target=lambda: mark_merged(ns_home, rb))
    b.start()
    b.join(timeout=0.3)
    assert b.is_alive(), "mark_merged must not load the forum before it holds forum.lock"
    release.set()
    a.join(timeout=5)
    b.join(timeout=5)
    assert {n["repo_name"]: n["merged"] for n in load_forum(ns_home)["nights"]} == {
        "A": True,
        "B": True,
    }
    with pytest.raises(RuntimeError, match="no write"):
        mutate_forum(ns_home, lambda doc: (_ for _ in ()).throw(RuntimeError("no write")))
    assert {n["repo_name"]: n["merged"] for n in load_forum(ns_home)["nights"]} == {
        "A": True,
        "B": True,
    }


def test_upsert_night_merged_stickiness():
    rid = "c0ffee12ab34"
    fresh = _night(rid, "night/2026-09-01", [], merged=False, merged_by="")
    # operator mark survives an incoming unmerged verdict
    forum = empty_forum()
    upsert_night(forum, _night(rid, "night/2026-09-01", [], merged=True, merged_by=MERGED_BY_OPERATOR))
    row = upsert_night(forum, dict(fresh))
    assert row["merged"] is True and row["merged_by"] == MERGED_BY_OPERATOR
    # a merged=True of unknown origin is left alone too
    forum = empty_forum()
    stored = _night(rid, "night/2026-09-01", [], merged=True)
    stored.pop("merged_by", None)
    upsert_night(forum, stored)
    row = upsert_night(forum, dict(fresh))
    assert row["merged"] is True and row["merged_by"] == ""
    # a git stamp is recomputed by an incoming verdict, kept by a row without one
    forum = empty_forum()
    upsert_night(forum, _night(rid, "night/2026-09-01", [], merged=True, merged_by=MERGED_BY_GIT))
    row = upsert_night(forum, {"repo_id": rid, "night": "night/2026-09-01", "landed": 3})
    assert row["merged"] is True and row["merged_by"] == MERGED_BY_GIT and row["landed"] == 3
    row = upsert_night(forum, dict(fresh))
    assert row["merged"] is False and row["merged_by"] == ""
    assert len(forum["nights"]) == 1
    # an operator stamp is never downgraded to "git", so a later unmerged
    # verdict cannot clear it (rule 3 alone keeps the night merged)
    forum = empty_forum()
    upsert_night(forum, _night(rid, "night/2026-09-01", [], merged=True, merged_by=MERGED_BY_OPERATOR))
    row = upsert_night(forum, _night(rid, "night/2026-09-01", [], merged=True, merged_by=MERGED_BY_GIT))
    assert row["merged"] is True and row["merged_by"] == MERGED_BY_OPERATOR
    row = upsert_night(forum, dict(fresh))
    assert row["merged"] is True and row["merged_by"] == MERGED_BY_OPERATOR
    # same for a stamp of unknown origin: nothing can recompute it, so keep it
    forum = empty_forum()
    upsert_night(forum, {"repo_id": rid, "night": "night/2026-09-01", "merged": True})
    upsert_night(forum, _night(rid, "night/2026-09-01", [], merged=True, merged_by=MERGED_BY_GIT))
    row = upsert_night(forum, dict(fresh))
    assert row["merged"] is True and row["merged_by"] == ""
    # an operator mark arriving over a git stamp does take over
    forum = empty_forum()
    upsert_night(forum, _night(rid, "night/2026-09-01", [], merged=True, merged_by=MERGED_BY_GIT))
    row = upsert_night(
        forum,
        {"repo_id": rid, "night": "night/2026-09-01", "merged": True, "merged_by": MERGED_BY_OPERATOR},
    )
    assert row["merged"] is True and row["merged_by"] == MERGED_BY_OPERATOR
