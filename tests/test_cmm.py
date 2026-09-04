from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from nightshift import cli, cmm
from nightshift.cmm import (
    LEVEL_NAMES,
    LEVELS,
    _checkout_at,
    histogram,
    load_cmm,
    population,
    render_cmm_html,
    render_cmm_md,
    score_repo,
    write_cmm,
)
from nightshift.config import Settings
from nightshift.demo import seed_widget
from nightshift.forum import (
    empty_forum,
    ingest_forum,
    load_forum,
    mark_merged,
    reuse_event_id,
    save_forum,
)
from nightshift.gitops import current_branch, git, rev_parse
from nightshift.ledger import check_hash, item_id, night_id, repo_id
from nightshift.runner import run_night
from nightshift.safety import is_nightshift_repo
from nightshift.status import request_halt

AINEKO_PATH = "M5 18c-2.8-1.2-3.2-6 1-6.2"
REAL_CHECKOUT = Path(__file__).resolve().parents[1]


def seed_fake_nightshift(dest: Path) -> Path:
    """A widget that passes is_nightshift_repo: pyproject name + src/nightshift/cli.py, committed on main."""
    repo = seed_widget(dest)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "nightshift"\nversion = "0.0.0"\n', encoding="utf-8"
    )
    (repo / "src" / "nightshift").mkdir(parents=True)
    (repo / "src" / "nightshift" / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "fake nightshift marker")
    assert is_nightshift_repo(repo)
    assert current_branch(repo) == "main"
    return repo


def _dated(ns_home: Path, day: int) -> Settings:
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
        "merged_by": "",
        "landed": 1,
        "voided": 0,
        "remaining": 0,
        "error": "",
        "mock": True,
        "brief_size": 2,
        "lens_hint": "",
        "item_ids": list(item_ids),
        "void_reasons": [],
    }
    row.update(over)
    return row


def _forum(items: list[dict], nights: list[dict], reuse: list[dict] | None = None) -> dict:
    doc = empty_forum()
    doc["items"] = items
    doc["nights"] = nights
    doc["reuse_events"] = list(reuse or [])
    return doc


def _kinds(score: dict) -> list[tuple[int, str]]:
    return [(e["level"], e["kind"]) for e in score["evidence"]]


def _no_git_evidence(monkeypatch) -> None:
    """L1-L4 are forum-only: any git read for merge evidence is a test failure."""

    def boom(*a, **k):
        raise AssertionError("git merge evidence consulted")

    monkeypatch.setattr("nightshift.cmm.strict_default_branch", boom)
    monkeypatch.setattr("nightshift.cmm.default_ledger_evidence", boom)
    monkeypatch.setattr("nightshift.cmm.night_merged", boom)


# --- L0: empty forum, including this checkout ---------------------------------------


def test_empty_forum_is_all_l0_including_this_checkout(tmp_path, monkeypatch):
    _no_git_evidence(monkeypatch)
    fake = seed_fake_nightshift(tmp_path / "nightshift")
    widget = seed_widget(tmp_path / "widget")
    assert is_nightshift_repo(REAL_CHECKOUT)  # read-only; nothing runs on it
    for repo in (fake, widget, REAL_CHECKOUT):
        score = score_repo(repo, empty_forum())
        assert score == {
            "repo_id": repo_id(repo),
            "repo_name": repo.name,
            "repo_path": str(repo),
            "level": 0,
            "evidence": [],
        }
    snap = histogram([fake, widget, REAL_CHECKOUT], empty_forum())
    assert snap["schema"] == 1 and snap["computed_at"] and snap["roots"] == []
    assert snap["histogram"] == {"L0": 3, "L1": 0, "L2": 0, "L3": 0, "L4": 0, "L5": 0}
    assert [r["level"] for r in snap["repos"]] == [0, 0, 0]
    assert LEVELS == ("L0", "L1", "L2", "L3", "L4", "L5")
    assert LEVEL_NAMES["L5"] == "meta RSI"
    # junk documents never raise
    assert score_repo(widget, {})["level"] == 0
    assert score_repo(widget, {"nights": "x", "items": [1], "reuse_events": None})["level"] == 0


def test_package_checkout_and_population(tmp_path, monkeypatch):
    # conftest (N7) patches package_checkout to None for the whole suite
    assert cmm.package_checkout() is None
    fake = seed_fake_nightshift(tmp_path / "nightshift")
    widget = seed_widget(tmp_path / "widget")
    assert _checkout_at(fake) == fake
    assert _checkout_at(widget) is None
    (tmp_path / "plain").mkdir()
    assert _checkout_at(tmp_path / "plain") is None
    assert _checkout_at(REAL_CHECKOUT) == REAL_CHECKOUT  # read-only marker check
    settings = Settings(roots=[tmp_path])
    assert population(settings) == [fake, widget]
    # the meta checkout joins the population only when it is not under roots
    monkeypatch.setattr("nightshift.cmm.package_checkout", lambda: fake)
    assert population(settings) == [fake, widget]
    other = seed_fake_nightshift(tmp_path.parent / f"{tmp_path.name}-meta")
    try:
        monkeypatch.setattr("nightshift.cmm.package_checkout", lambda: other)
        assert population(settings) == [fake, widget, other]
    finally:
        shutil.rmtree(other)


# --- L1 / L2 / L3 from live mock nights ---------------------------------------------


def test_halt_before_host_is_l1_not_l2(fixture_repo, mock_settings, ns_home, monkeypatch):
    _no_git_evidence(monkeypatch)
    request_halt(ns_home, os.getpid())
    report = run_night(fixture_repo, mock_settings, explicit=True)
    assert report.halt_reason == "requested"
    forum = load_forum(ns_home)
    assert all(i["attempted"] is False for i in forum["items"])
    ledger = json.loads((fixture_repo / ".nightshift" / "ledger.json").read_text())
    assert all(e.get("last_exit") == 0 for e in ledger["entries"])  # present, never a predicate
    score = score_repo(fixture_repo, forum)
    assert score["level"] == 1
    assert score["evidence"] == [{"level": 1, "kind": "freeze", "night": report.branch}]


def test_mock_night_is_l2_and_history_void_night_is_l3(fixture_repo, ns_home, monkeypatch):
    _no_git_evidence(monkeypatch)
    r1 = run_night(fixture_repo, _dated(ns_home, 3), explicit=True)
    forum = load_forum(ns_home)
    score = score_repo(fixture_repo, forum)
    assert score["level"] == 2
    assert _kinds(score) == [(1, "freeze"), (2, "host_check")]
    assert score["evidence"][1]["night"] == r1.branch
    assert score["evidence"][1]["item_id"] in forum["nights"][0]["item_ids"]
    assert forum["nights"][0]["void_reasons"] == []
    # night 2 voids both keys from history; the done items are kept, the night
    # row records why it voided, and that memory is L3 (no host_check tonight)
    r2 = run_night(fixture_repo, _dated(ns_home, 4), explicit=True)
    assert all(u.void_reason == "duplicate_of_history" for u in r2.brief.upgrades)
    forum = load_forum(ns_home)
    by_night = {n["night"]: n for n in forum["nights"]}
    assert by_night[r2.branch]["void_reasons"] == ["duplicate_of_history"] * 2
    assert by_night[r1.branch]["void_reasons"] == []
    assert all(i["done"] and i["void_reason"] == "" for i in forum["items"])
    score = score_repo(fixture_repo, forum)
    assert score["level"] == 3
    assert _kinds(score) == [(1, "freeze"), (2, "host_check"), (3, "history_void")]
    assert score["evidence"][2] == {"level": 3, "kind": "history_void", "night": r2.branch}
    # a morning ingest is additive on live rows: the memory stays
    forum = ingest_forum(ns_home, [fixture_repo])
    assert {n["night"]: n["void_reasons"] for n in forum["nights"]} == {
        r1.branch: [],
        r2.branch: ["duplicate_of_history"] * 2,
    }
    assert score_repo(fixture_repo, forum)["level"] == 3
    snap = histogram([fixture_repo], forum)
    assert snap["histogram"] == {"L0": 0, "L1": 0, "L2": 0, "L3": 1, "L4": 0, "L5": 0}


def test_l3_from_failed_before_item_without_any_attempted_item(tmp_path, monkeypatch):
    _no_git_evidence(monkeypatch)
    repo = seed_widget(tmp_path / "widget")
    rid = repo_id(repo)
    lesson = _item(
        rid, "night/2026-09-02", "retry", "pytest -q", ["widget.py"],
        attempted=False, done=False, voided=True, void_reason="failed_before:night/x note",
    )
    night = _night(rid, "night/2026-09-02", [lesson["id"]], halt_reason="halted", landed=0, voided=1)
    score = score_repo(repo, _forum([lesson], [night]))
    assert score["level"] == 3
    assert _kinds(score) == [(1, "freeze"), (3, "history_void")]
    assert score["evidence"][1]["item_id"] == lesson["id"]
    assert score["evidence"][1]["night"] == "night/2026-09-02"
    # duplicate_of_history is a lesson too; dirty_in_tree is not
    dup = dict(lesson, void_reason="duplicate_of_history")
    assert score_repo(repo, _forum([dup], [night]))["level"] == 3
    dirty = dict(lesson, void_reason="dirty_in_tree")
    assert score_repo(repo, _forum([dirty], [night]))["level"] == 1
    # L3 chains from L1: a lesson item without any freeze night is still L0
    assert score_repo(repo, _forum([lesson], []))["level"] == 0
    # error stubs never count as a freeze; an error night with items does
    stub = _night(rid, "error/2026-09-02T01:00:00", [], branch="", halt_reason="error")
    assert score_repo(repo, _forum([], [stub]))["level"] == 0
    crashed = _night(rid, "night/2026-09-03", [dirty["id"]], halt_reason="error", error="boom")
    assert score_repo(repo, _forum([dirty], [crashed]))["level"] == 1
    # a night that halted with no items (never froze a stub) counts too
    assert score_repo(repo, _forum([], [_night(rid, "night/2026-09-04", [], halt_reason="clock")]))["level"] == 1
    # L2 is `attempted is True` only, never a truthy last_exit
    tried = _item(rid, "night/2026-09-05", "t", "pytest -q x", ["widget.py"], done=False, attempted=False, last_exit=1)
    assert score_repo(repo, _forum([tried], [_night(rid, "night/2026-09-05", [tried["id"]])]))["level"] == 1
    assert score_repo(repo, _forum([dict(tried, attempted=True)], [_night(rid, "night/2026-09-05", [tried["id"]])]))["level"] == 2


# --- L4: consumer-only, attempted / applied only --------------------------------------


def test_l4_only_for_consumer_from_attempted_or_applied_reuse(tmp_path, monkeypatch):
    _no_git_evidence(monkeypatch)
    origin_repo = seed_widget(tmp_path / "alpha")
    consumer_repo = seed_widget(tmp_path / "beta")
    rid_a, rid_b = repo_id(origin_repo), repo_id(consumer_repo)
    cmd, paths = "pytest tests/test_widget.py::test_add -q", ["widget.py"]
    origin = _item(rid_a, "night/2026-09-01", "Make test_add pass", cmd, paths, repo_name="alpha")
    consumer = _item(rid_b, "night/2026-09-02", "Make test_add pass", cmd, paths, repo_name="beta")
    nights = [
        _night(rid_a, "night/2026-09-01", [origin["id"]], repo_name="alpha"),
        _night(rid_b, "night/2026-09-02", [consumer["id"]], repo_name="beta"),
    ]

    def event(kind: str) -> dict:
        return {
            "id": reuse_event_id(origin["id"], consumer["id"], "night/2026-09-02", kind),
            "at": "2026-09-02T03:00:00+00:00",
            "kind": kind,
            "origin_repo_id": rid_a,
            "origin_repo_name": "alpha",
            "origin_item_id": origin["id"],
            "consumer_repo_id": rid_b,
            "consumer_repo_name": "beta",
            "consumer_night": "night/2026-09-02",
            "consumer_item_id": consumer["id"],
            "match": "check_hash+paths",
        }

    for kind in ("applied", "attempted"):
        forum = _forum([origin, consumer], nights, [event(kind)])
        score = score_repo(consumer_repo, forum)
        assert score["level"] == 4
        assert score["evidence"][-1] == {
            "level": 4,
            "kind": "forum_reuse",
            "night": "night/2026-09-02",
            "event_id": event(kind)["id"],
        }
        assert score_repo(origin_repo, forum)["level"] == 2  # an origin stays at its own level
    proposed = _forum([origin, consumer], nights, [event("proposed")])
    assert score_repo(consumer_repo, proposed)["level"] == 2
    assert score_repo(origin_repo, proposed)["level"] == 2
    snap = histogram([origin_repo, consumer_repo], _forum([origin, consumer], nights, [event("applied")]))
    assert snap["histogram"] == {"L0": 0, "L1": 0, "L2": 1, "L3": 0, "L4": 1, "L5": 0}


def test_two_widget_nights_give_the_consumer_l4(tmp_path, mock_settings, ns_home, monkeypatch):
    _no_git_evidence(monkeypatch)
    alpha = seed_widget(tmp_path / "alpha")
    beta = seed_widget(tmp_path / "beta")
    run_night(alpha, mock_settings, explicit=True)
    run_night(beta, mock_settings, explicit=True)
    forum = load_forum(ns_home)
    assert {e["kind"] for e in forum["reuse_events"]} == {"applied"}
    assert score_repo(alpha, forum)["level"] == 2
    assert score_repo(beta, forum)["level"] == 4
    snap = histogram([alpha, beta], forum)
    assert snap["histogram"] == {"L0": 0, "L1": 0, "L2": 1, "L3": 0, "L4": 1, "L5": 0}


# --- L5: meta + done + merged (git or mark-merged), never a widget -----------------------


def test_l5_fake_nightshift_needs_merge_evidence(tmp_path, mock_settings, ns_home):
    fake = seed_fake_nightshift(tmp_path / "nightshift")
    main_sha = rev_parse(fake, "main")
    report = run_night(fake, mock_settings, explicit=True)
    assert report.landed == 2 and report.halt_reason == "remaining_zero"
    forum = ingest_forum(ns_home, [fake])
    assert forum["nights"][0]["meta"] is True and forum["nights"][0]["merged"] is False
    assert all(i["done"] for i in forum["items"])
    score = score_repo(fake, forum)
    assert score["level"] == 2  # done on an unmerged branch is not L5
    assert _kinds(score) == [(1, "freeze"), (2, "host_check")]
    assert rev_parse(fake, "main") == main_sha
    # land the night: live git evidence counts even before the next ingest
    git(fake, "checkout", "main")
    git(fake, "merge", "--no-ff", "-m", "land", report.branch)
    score = score_repo(fake, load_forum(ns_home))
    assert score["level"] == 5
    assert score["evidence"][-1]["kind"] == "meta_merged"
    assert score["evidence"][-1]["night"] == report.branch
    assert score["evidence"][-1]["item_id"] in forum["nights"][0]["item_ids"]
    forum = ingest_forum(ns_home, [fake])
    assert forum["nights"][0]["merged"] is True and forum["nights"][0]["merged_by"] == "git"
    score = score_repo(fake, forum)
    assert score["level"] == 5
    assert _kinds(score) == [(1, "freeze"), (2, "host_check"), (5, "meta_merged")]
    snap = histogram([fake], forum)
    assert snap["histogram"] == {"L0": 0, "L1": 0, "L2": 0, "L3": 0, "L4": 0, "L5": 1}
    assert snap["repos"][0]["level"] == 5


def test_l5_from_mark_merged_without_merging(tmp_path, mock_settings, ns_home):
    fake = seed_fake_nightshift(tmp_path / "nightshift")
    main_sha = rev_parse(fake, "main")
    report = run_night(fake, mock_settings, explicit=True)
    forum = load_forum(ns_home)
    assert score_repo(fake, forum)["level"] == 2
    forum = mark_merged(ns_home, fake)
    assert forum["nights"][0]["merged_by"] == "operator"
    score = score_repo(fake, forum)
    assert score["level"] == 5
    assert score["evidence"][-1] == {
        "level": 5,
        "kind": "meta_merged",
        "night": report.branch,
        "item_id": score["evidence"][-1]["item_id"],
    }
    assert current_branch(fake) == report.branch and rev_parse(fake, "main") == main_sha


def test_widget_with_done_and_merged_stays_below_l5(tmp_path, monkeypatch):
    _no_git_evidence(monkeypatch)  # a widget never consults git for L5
    widget = seed_widget(tmp_path / "widget")
    rid = repo_id(widget)
    done = _item(rid, "night/2026-09-01", "a", "pytest -q", ["widget.py"])
    merged = _night(rid, "night/2026-09-01", [done["id"]], merged=True, merged_by="operator")
    score = score_repo(widget, _forum([done], [merged]))
    assert score["level"] == 2 and _kinds(score) == [(1, "freeze"), (2, "host_check")]
    lesson = _item(rid, "night/2026-09-02", "b", "pytest -q b", ["widget.py"], done=False, voided=True, void_reason="failed_before:x")
    assert score_repo(widget, _forum([done, lesson], [merged]))["level"] == 3


def test_l5_never_from_a_home_shard_or_head_on_main(tmp_path, mock_settings, ns_home):
    fake = seed_fake_nightshift(tmp_path / "nightshift")
    report = run_night(fake, mock_settings, explicit=True)
    git(fake, "checkout", "main")
    git(fake, "branch", "-D", report.branch)  # dropped: only the home shard remembers
    assert not (fake / ".nightshift" / "ledger.json").exists()
    assert (ns_home / "ledger" / f"{repo_id(fake)}.json").is_file()
    forum = ingest_forum(ns_home, [fake])
    assert forum["nights"][0]["merged"] is False
    assert score_repo(fake, forum)["level"] == 2


# --- histogram: once per repo at its max, gone clones omitted ----------------------------


def test_histogram_counts_once_at_max_and_omits_gone_paths(tmp_path, ns_home, monkeypatch):
    _no_git_evidence(monkeypatch)
    a = seed_widget(tmp_path / "a")
    b = seed_widget(tmp_path / "b")
    gone = seed_widget(tmp_path / "gone")
    rid_a, rid_gone = repo_id(a), repo_id(gone)
    tried = _item(rid_a, "night/2026-09-01", "t", "pytest -q", ["widget.py"], done=False)
    lesson = _item(rid_a, "night/2026-09-02", "l", "pytest -q l", ["widget.py"], attempted=False, done=False, voided=True, void_reason="failed_before:night/2026-09-01")
    ghost = _item(rid_gone, "night/2026-09-01", "g", "pytest -q", ["widget.py"])
    forum = _forum(
        [tried, lesson, ghost],
        [
            _night(rid_a, "night/2026-09-01", [tried["id"]]),
            _night(rid_a, "night/2026-09-02", [lesson["id"]]),
            _night(rid_gone, "night/2026-09-01", [ghost["id"]], repo_path=str(gone)),
        ],
    )
    shutil.rmtree(gone)
    snap = histogram([a, b, gone, a, Path(str(a) + "/")], forum, roots=[tmp_path])
    assert snap["roots"] == [str(tmp_path)]
    assert [r["repo_path"] for r in snap["repos"]] == [str(a), str(b)]
    assert [r["level"] for r in snap["repos"]] == [3, 0]
    assert snap["histogram"] == {"L0": 1, "L1": 0, "L2": 0, "L3": 1, "L4": 0, "L5": 0}
    assert sum(snap["histogram"].values()) == 2
    path = write_cmm(ns_home, snap)
    assert path == ns_home / "cmm.json"
    assert load_cmm(ns_home) == json.loads(path.read_text(encoding="utf-8")) == snap
    assert (ns_home / "cmm.html").is_file()
    assert not (ns_home / "cmm.json.tmp").exists() and not (ns_home / "cmm.html.tmp").exists()
    (ns_home / "cmm.json").write_text("{nope", encoding="utf-8")
    assert load_cmm(ns_home) is None


# --- render: md + html -------------------------------------------------------------------


def _rects(html: str) -> dict[str, str]:
    return {m.group(1): m.group(0) for m in re.finditer(r'<rect data-level="(L\d)"[^>]*/>', html)}


def test_render_cmm_html_has_no_webfonts_and_dashes_empty_columns():
    snap = {
        "schema": 1,
        "computed_at": "2026-09-04T07:00:00+00:00",
        "roots": ["/tmp/roots"],
        "histogram": {"L0": 3, "L1": 0, "L2": 1, "L3": 0, "L4": 0, "L5": 0},
        "repos": [
            {"repo_id": "a" * 12, "repo_name": "<b>alpha</b>", "repo_path": "/tmp/roots/<alpha>", "level": 2,
             "evidence": [{"level": 1, "kind": "freeze", "night": "n"}, {"level": 2, "kind": "host_check", "night": "n", "item_id": "i"}]},
            {"repo_id": "b" * 12, "repo_name": "beta", "repo_path": "/tmp/roots/beta", "level": 0, "evidence": []},
        ],
    }
    html = render_cmm_html(snap)
    assert "fonts.googleapis" not in html and "http" not in html.split("<style>")[1].split("</style>")[0]
    assert AINEKO_PATH in html and 'stroke="#c44928"' in html
    assert "--paper: #e8dfd2" in html and "--ink: #14110e" in html and "--accent: #c44928" in html
    assert "--soft: #a39a8e" in html and "--muted: #6f675e" in html
    assert "Geist" not in html and "Instrument" not in html
    assert "ui-serif" in html and "system-ui" in html and "ui-monospace" in html
    rects = _rects(html)
    assert set(rects) == set(LEVELS)
    assert "stroke-dasharray" not in rects["L0"] and "stroke-dasharray" not in rects["L2"]
    for level in ("L1", "L3", "L4", "L5"):
        assert "stroke-dasharray" in rects[level] and 'height="12"' in rects[level]
    assert 'height="232"' in rects["L0"] and 'height="77"' in rects["L2"]
    assert 'fill="rgba(196,73,40,0.10)"' in rects["L0"]
    counts = {m.group(1): m.group(2) for m in re.finditer(r'<text data-count="(L\d)"[^>]*>(\d+)</text>', html)}
    assert counts == {"L0": "3", "L1": "0", "L2": "1", "L3": "0", "L4": "0", "L5": "0"}
    for level in LEVELS:
        assert f">{LEVEL_NAMES[level]}</text>" in html
    # per-repo table, escaped
    assert "&lt;b&gt;alpha&lt;/b&gt;" in html and "<b>alpha</b>" not in html
    assert "/tmp/roots/&lt;alpha&gt;" in html
    assert '<td class="level">L2</td><td class="num">2</td>' in html
    assert '<td class="level">L0</td><td class="num">0</td>' in html
    assert "2026-09-04T07:00:00+00:00" in html and "4 REPOS" in html
    # an empty snapshot: six dashed columns, no rows, never raises
    empty = render_cmm_html({"histogram": {}, "repos": "junk"})
    assert all("stroke-dasharray" in r for r in _rects(empty).values()) and "(no repos)" in empty
    assert "fonts.googleapis" not in empty


def test_render_cmm_md_lines():
    snap = {
        "computed_at": "2026-09-04T07:00:00+00:00",
        "histogram": {"L0": 2, "L1": 0, "L2": 1, "L3": 0, "L4": 0, "L5": 0},
        "repos": [
            {"repo_name": "beta", "repo_path": "/r/beta", "level": 0, "evidence": []},
            {"repo_name": "alpha", "repo_path": "/r/alpha", "level": 2, "evidence": [{}, {}]},
        ],
    }
    text = render_cmm_md(snap)
    lines = text.splitlines()
    assert lines[0] == "# Nightshift CMM"
    assert lines[1] == "Aineko · evidence histogram · not a score."
    assert lines[2] == "Computed 2026-09-04T07:00:00+00:00  repos 3"
    assert lines[4] == "L0  unobserved      " + "#" * 24 + "  2"
    assert lines[5] == "L1  checkable DE    " + " " * 24 + "  0"
    assert lines[6] == "L2  nights with OE  " + "#" * 12 + " " * 12 + "  1"
    assert lines[9] == "L5  meta RSI        " + " " * 24 + "  0"
    assert lines[11:] == ["alpha  L2  /r/alpha", "beta   L0  /r/beta"]
    assert text.endswith("\n")
    assert render_cmm_md({}).splitlines()[-1] == "- (no repos)"


# --- CLI: cmm, morning --portfolio, forum mark-merged ------------------------------------


def test_cli_cmm_json_and_files(tmp_path, ns_home, capsys):
    widget = seed_widget(tmp_path / "widget")
    assert cli.main(["cmm", "--json", "--roots", str(tmp_path)]) == 0
    snap = json.loads(capsys.readouterr().out)
    assert snap["histogram"] == {"L0": 1, "L1": 0, "L2": 0, "L3": 0, "L4": 0, "L5": 0}
    assert snap["roots"] == [str(tmp_path)]
    assert [r["repo_path"] for r in snap["repos"]] == [str(widget)]
    assert (ns_home / "cmm.json").is_file() and (ns_home / "cmm.html").is_file()
    assert json.loads((ns_home / "cmm.json").read_text(encoding="utf-8")) == snap
    html = (ns_home / "cmm.html").read_text(encoding="utf-8")
    assert "fonts.googleapis" not in html and AINEKO_PATH in html
    assert cli.main(["cmm", "--roots", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "# Nightshift CMM" in out and "L0  unobserved" in out
    assert f"widget  L0  {widget}" in out


def test_cli_morning_portfolio_and_repo_required(fixture_repo, mock_settings, ns_home, capsys, tmp_path):
    assert cli.main(["morning"]) == 1
    assert "repo required" in capsys.readouterr().err
    assert cli.main(["morning", "--portfolio", "--roots", str(tmp_path)]) == 1
    assert "no forum yet" in capsys.readouterr().err
    assert not (ns_home / "cmm.json").exists()
    report = run_night(fixture_repo, mock_settings, explicit=True)
    assert cli.main(["morning", "--portfolio", "--roots", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Nightshift forum\nAineko · portfolio ledger · not a chat.\n")
    assert "[done] Make test_add pass" in out
    assert "\n\n# Nightshift CMM\n" in out
    assert "L2  nights with OE  " + "#" * 24 + "  1" in out
    assert f"widget  L2  {fixture_repo}" in out
    land = out.rsplit("## Land\n", 1)[1]
    assert land == (
        f"- widget: `git checkout main && git merge --no-ff {report.branch}`\n"
        f"- widget: `git branch -D {report.branch}`\n"
    )
    assert (ns_home / "cmm.json").is_file() and (ns_home / "cmm.html").is_file()
    # --diff is per-repo and ignored with --portfolio
    assert cli.main(["morning", "--portfolio", "--diff", "--roots", str(tmp_path)]) == 0
    assert "diff --git" not in capsys.readouterr().out
    # the per-repo read is unchanged
    assert cli.main(["morning", str(fixture_repo)]) == 0
    assert "merge --no-ff" in capsys.readouterr().out


def test_cli_forum_mark_merged_then_cmm_shows_l5(tmp_path, mock_settings, ns_home, capsys):
    fake = seed_fake_nightshift(tmp_path / "nightshift")
    assert cli.main(["forum", "mark-merged", str(fake)]) == 1
    assert "no forum nights" in capsys.readouterr().err
    report = run_night(fake, mock_settings, explicit=True)
    assert cli.main(["cmm", "--roots", str(tmp_path)]) == 0
    assert f"nightshift  L2  {fake}" in capsys.readouterr().out
    assert cli.main(["forum", "mark-merged", str(fake), "night/1999-01-01"]) == 1
    assert "no forum night 'night/1999-01-01'" in capsys.readouterr().err
    assert load_forum(ns_home)["nights"][0]["merged"] is False
    assert cli.main(["forum", "mark-merged", str(fake)]) == 0
    assert capsys.readouterr().out == f"merged\tnightshift\t{report.branch}\n"
    night = load_forum(ns_home)["nights"][0]
    assert night["merged"] is True and night["merged_by"] == "operator"
    assert cli.main(["cmm", "--roots", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert f"nightshift  L5  {fake}" in out and "L5  meta RSI        " + "#" * 24 + "  1" in out
    assert json.loads((ns_home / "cmm.json").read_text(encoding="utf-8"))["histogram"]["L5"] == 1
    # nothing left to stamp: a typo must not mark the whole history
    assert cli.main(["forum", "mark-merged", str(fake)]) == 1
    assert "no unmerged forum night" in capsys.readouterr().err
    assert current_branch(fake) == report.branch
    # explicit night on an already-merged row is idempotent
    assert cli.main(["forum", "mark-merged", str(fake), report.branch]) == 0
    assert capsys.readouterr().out == f"merged\tnightshift\t{report.branch}\n"
    # save_forum + the html page agree on the level
    save_forum(ns_home, load_forum(ns_home))
    assert cli.main(["cmm", "--json", "--roots", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["repos"][0]["level"] == 5
