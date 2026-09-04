from __future__ import annotations

from pathlib import Path

from nightshift.demo import seed_widget
from nightshift.forum import (
    FORUM_BLOCK_HEADING,
    FORUM_BLOCK_INSTRUCTION,
    TRUNCATED_MARK,
    forum_match,
    forum_snapshot_block,
    ingest_forum,
    load_forum,
    publish_error_stub,
    publish_night,
    reuse_event_id,
    reuse_events_for_night,
    save_forum,
)
from nightshift.graph import LoopNodes, NightContext
from nightshift.ledger import check_hash, item_id, load_ledger, night_id, repo_id, save_ledger
from nightshift.llm import Critic, MockChatClient, Writer, mock_upgrades_from_repo
from nightshift.models import Brief, Upgrade
from nightshift.runner import NightReport, freeze_snapshot, run_night
from nightshift.status import StatusBoard

RID_A = "a" * 12
RID_B = "b" * 12
RID_C = "c" * 12
SHLEX_TITLE = "quote host checks with shlex"
SHLEX_CMD = "pytest tests/test_host_shell_abs_path.py -q"
SHLEX_PATHS = ["src/nightshift/host.py", "tests/test_host_shell_abs_path.py"]


def _item(rid: str, name: str, night: str, title: str, cmd: str, paths: list[str], **over) -> dict:
    row = {
        "id": item_id(rid, check_hash(cmd), paths),
        "repo_id": rid,
        "repo_name": name,
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


def _night(rid: str, name: str, night: str, ended_at: str, item_ids: list[str]) -> dict:
    return {
        "id": night_id(rid, night),
        "repo_id": rid,
        "repo_name": name,
        "repo_path": f"/tmp/{name}",
        "meta": False,
        "night": night,
        "branch": night,
        "started_at": ended_at[:10] + "T01:00:00" if ended_at else "",
        "ended_at": ended_at,
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


def _forum(items: list[dict], nights: list[dict]) -> dict:
    return {
        "schema": 1,
        "updated_at": "",
        "nights": nights,
        "items": items,
        "reuse_events": [],
        "errors": [],
    }


def _shlex_forum() -> tuple[dict, dict]:
    """Repo A's landed shlex catch and its night."""
    shlex = _item(RID_A, "nightshift", "night/2026-09-01", SHLEX_TITLE, SHLEX_CMD, SHLEX_PATHS)
    night = _night(RID_A, "nightshift", "night/2026-09-01", "2026-09-01T04:10:00", [shlex["id"]])
    return shlex, night


def _rows(block: str) -> list[str]:
    return [ln for ln in block.splitlines() if ln.startswith("- ")]


def _ctx(repo: Path, settings, home: Path) -> NightContext:
    return NightContext(
        repo=repo,
        settings=settings,
        writer=Writer(MockChatClient("writer", repo), repo),
        critic=Critic(MockChatClient("critic", repo), repo),
        status=StatusBoard(home),
        clock=settings.now_fn,
        deadline=settings.now_fn(),
    )


# --- (a) ranking + 8 KB cap: a landed keeper survives 200 later noise rows -------


def test_excerpt_keeps_done_shlex_row_above_200_later_noise_items():
    shlex, shlex_night = _shlex_forum()
    items, nights = [shlex], [shlex_night]
    for i in range(200):
        rid = f"{i:012x}"
        night = f"night/2026-09-{2 + i % 27:02d}"
        cmd = f"pytest tests/test_noise_{i}.py -q"
        over: dict = {"done": False}
        if i % 3 == 0:
            over.update(attempted=True, last_exit=1)
        elif i % 3 == 1:
            over.update(attempted=False, voided=True, void_reason="dirty_in_tree")
        else:
            over.update(attempted=False, voided=True, void_reason="failed_before:night/2026-09-01 x")
        row = _item(rid, f"repo{i}", night, f"noise upgrade {i} harden module {i}", cmd, [f"src/noise_{i}.py"], **over)
        items.append(row)
        nights.append(_night(rid, f"repo{i}", night, f"2026-09-{2 + i % 27:02d}T05:00:00", [row["id"]]))
    # noise first in the file: order of items[] must not matter
    forum = _forum(list(reversed(items)), nights)
    block = forum_snapshot_block(forum, exclude_repo_id="z" * 12)
    assert block.startswith(FORUM_BLOCK_HEADING + "\n\n" + FORUM_BLOCK_INSTRUCTION + "\n\n- ")
    assert "JOBS N" in FORUM_BLOCK_INSTRUCTION
    assert SHLEX_TITLE in block
    assert len(block.encode("utf-8")) <= 8192
    assert block.splitlines()[-1] == TRUNCATED_MARK == "… truncated"
    rows = _rows(block)
    assert rows[0] == f"- [done] nightshift: {SHLEX_TITLE}"
    assert f"  check: `{SHLEX_CMD}`" in block
    # lessons (failed_before / duplicate_of_history) rank right after done rows
    assert rows[1].startswith("- [void failed_before:night/2026-09-01 x] repo")
    # rows are never split by the cut: every title line keeps its check line
    assert len(rows) == block.count("  check: `")
    assert 1 < len(rows) < 201
    # no cap: every row, no marker, done > lessons > the rest, newest night first within a tier
    full = forum_snapshot_block(forum, exclude_repo_id="z" * 12, max_bytes=1_000_000)
    full_rows = _rows(full)
    assert len(full_rows) == 201 and TRUNCATED_MARK not in full
    assert full_rows[0] == rows[0]
    tiers = [0 if "[done]" in r else (1 if "failed_before" in r else 2) for r in full_rows]
    assert tiers == sorted(tiers) and tiers.count(0) == 1 and tiers.count(1) == 66
    lessons = [r for r in full_rows if "failed_before" in r]
    nights_of = {i["title"]: i["night"] for i in items}
    ended = [nights_of[r.split(": ", 1)[1]] for r in lessons]
    assert ended == sorted(ended, reverse=True)
    # the capped block is a prefix of the uncapped ranking
    assert full_rows[: len(rows)] == rows
    # an absurd cap still honours the byte limit and ends on the marker
    tiny = forum_snapshot_block(forum, exclude_repo_id="z" * 12, max_bytes=40)
    assert len(tiny.encode("utf-8")) <= 40 and tiny.splitlines()[-1] == TRUNCATED_MARK


# --- (b) dedup picks the best row per origin key, not the newest ------------------


def test_excerpt_dedups_same_key_to_the_done_row_not_the_later_void():
    shlex, shlex_night = _shlex_forum()
    later_void = _item(
        RID_C, "other", "night/2026-09-05", SHLEX_TITLE, SHLEX_CMD, SHLEX_PATHS,
        attempted=True, done=False, voided=True, void_reason="same_host_failure", last_exit=1,
    )
    later_attempt = _item(
        RID_B, "third", "night/2026-09-06", "shlex again", SHLEX_CMD, list(reversed(SHLEX_PATHS)),
        attempted=True, done=False, last_exit=2,
    )
    nights = [
        shlex_night,
        _night(RID_C, "other", "night/2026-09-05", "2026-09-05T04:00:00", [later_void["id"]]),
        _night(RID_B, "third", "night/2026-09-06", "2026-09-06T04:00:00", [later_attempt["id"]]),
    ]
    for order in ([later_void, later_attempt, shlex], [shlex, later_void, later_attempt]):
        block = forum_snapshot_block(_forum(list(order), nights), exclude_repo_id="z" * 12)
        rows = _rows(block)
        assert rows == [f"- [done] nightshift: {SHLEX_TITLE}"]
        assert "same_host_failure" not in block and "shlex again" not in block
        assert block.count(SHLEX_CMD) == 1
        assert TRUNCATED_MARK not in block
    # same repo, same key, a later void row sitting beside the done one (hand-edited file)
    same_repo_void = dict(later_void, repo_id=RID_A, repo_name="nightshift", night="night/2026-09-07")
    block = forum_snapshot_block(_forum([same_repo_void, shlex], nights), exclude_repo_id="z" * 12)
    assert _rows(block) == [f"- [done] nightshift: {SHLEX_TITLE}"]
    # without the done row, the newest lesson-less row is what remains, marked by its state
    block = forum_snapshot_block(_forum([later_void, later_attempt], nights), exclude_repo_id="z" * 12)
    assert _rows(block) == ["- [attempted] third: shlex again"]


# --- (c) own repo excluded; secrets never rendered; junk tolerated ----------------


def test_excerpt_excludes_own_repo_and_blocked_paths():
    shlex, shlex_night = _shlex_forum()
    env_row = _item(RID_B, "loopscope", "night/2026-09-02", "rotate keys in env", "pytest -q", ["app.py", ".env"])
    clean_b = _item(RID_B, "loopscope", "night/2026-09-02", "loopscope keeper", "pytest tests/test_b.py -q", ["app.py"])
    open_b = _item(
        RID_B, "loopscope", "night/2026-09-02", "never ran", "pytest tests/test_open.py -q", ["open.py"],
        attempted=False, done=False,
    )
    nights = [shlex_night, _night(RID_B, "loopscope", "night/2026-09-02", "2026-09-02T04:00:00", [])]
    forum = _forum([shlex, env_row, clean_b, open_b], nights)
    block_for_a = forum_snapshot_block(forum, exclude_repo_id=RID_A)
    assert "loopscope keeper" in block_for_a and "- [open] loopscope: never ran" in block_for_a
    assert SHLEX_TITLE not in block_for_a
    assert "rotate keys" not in block_for_a and ".env" not in block_for_a
    block_for_b = forum_snapshot_block(forum, exclude_repo_id=RID_B)
    assert _rows(block_for_b) == [f"- [done] nightshift: {SHLEX_TITLE}"]
    everyone = forum_snapshot_block(forum, exclude_repo_id="")
    assert SHLEX_TITLE in everyone and "loopscope keeper" in everyone
    assert ".env" not in everyone and "rotate keys" not in everyone
    # only this repo's rows, or no rows at all: no block
    assert forum_snapshot_block(_forum([shlex], [shlex_night]), exclude_repo_id=RID_A) == ""
    assert forum_snapshot_block(_forum([], []), exclude_repo_id=RID_A) == ""
    assert forum_snapshot_block({}, exclude_repo_id=RID_A) == ""
    # junk rows never raise: scalar lists, non-dict rows, a title with newlines
    weird = _item(RID_C, "weird", "night/2026-09-03", "multi\nline\ttitle", "pytest   -q  tests", ["w.py"])
    junk = _forum([weird, "junk", {"repo_id": RID_C, "paths": "w.py", "title": "scalar paths"}], "nope")
    block = forum_snapshot_block(junk, exclude_repo_id=RID_A)
    assert "- [done] weird: multi line title" in block and "  check: `pytest -q tests`" in block
    assert "scalar paths" in block


# --- (d) freeze snapshot carries the block; NIGHTSHIFT_FORUM=0 and the writer do not


def test_freeze_snapshot_has_forum_block_writer_does_not(fixture_repo, mock_settings, ns_home, monkeypatch):
    shlex, shlex_night = _shlex_forum()
    save_forum(ns_home, _forum([shlex], [shlex_night]))
    save_ledger(
        fixture_repo,
        {
            "entries": [
                {
                    "title": "own prior",
                    "check_command": "pytest tests/test_widget.py -q",
                    "paths": ["widget.py"],
                    "check_hash": check_hash("pytest tests/test_widget.py -q"),
                    "night": "night/2026-08-30",
                    "attempted": True,
                    "done": False,
                    "voided": False,
                    "void_reason": "",
                    "last_exit": 1,
                    "turns": 2,
                }
            ]
        },
        home=ns_home,
    )
    ctx = _ctx(fixture_repo, mock_settings, ns_home)
    snap = freeze_snapshot(ctx)
    assert "## Portfolio forum (other repos)" in snap
    assert SHLEX_TITLE in snap and FORUM_BLOCK_INSTRUCTION in snap
    ledger_at = snap.index("## Prior night ledger")
    forum_at = snap.index("## Portfolio forum")
    file_at = snap.index("## file ")
    assert ledger_at < forum_at < file_at
    assert "own prior" in snap[ledger_at:forum_at]
    # the excerpt is other repos only: this clone's own forum rows never appear
    own = _item(repo_id(fixture_repo), fixture_repo.name, "night/2026-09-02", "own forum row", "pytest -q x", ["widget.py"])
    save_forum(ns_home, _forum([own], []))
    assert "## Portfolio forum" not in freeze_snapshot(ctx)
    save_forum(ns_home, _forum([shlex, own], [shlex_night]))
    again = freeze_snapshot(ctx)
    assert SHLEX_TITLE in again and "own forum row" not in again
    # forum off: no excerpt, ledger block untouched
    monkeypatch.setenv("NIGHTSHIFT_FORUM", "0")
    off = freeze_snapshot(ctx)
    assert "## Portfolio forum" not in off and SHLEX_TITLE not in off
    assert "## Prior night ledger" in off and "own prior" in off
    monkeypatch.delenv("NIGHTSHIFT_FORUM")
    # the writer node passes home= only, never forum=
    captured: dict = {}

    def fake_snapshot(repo, **kwargs):
        captured.update(kwargs)
        return "# snap\n"

    monkeypatch.setattr("nightshift.graph.read_snapshot", fake_snapshot)
    brief = Brief.freeze(
        [
            Upgrade(1, "a", "python -m pytest tests/test_widget.py::test_add -q", ["widget.py"]),
            Upgrade(2, "b", "true", ["README.md"]),
        ]
    )
    LoopNodes(ctx).writer(
        {"brief": brief.to_dict(), "job": "x", "job_upgrade_id": 1, "turn": 1, "job_feedback": {}}
    )
    assert captured.get("home") == ns_home
    assert "forum" not in captured


# --- forum_match: exact key across repos, done preferred ------------------------------


def test_forum_match_exact_key_prefers_done_requires_attempted():
    own = _item(RID_A, "alpha", "night/2026-09-04", SHLEX_TITLE, SHLEX_CMD, SHLEX_PATHS, attempted=False, done=False)
    tried = _item(RID_B, "beta", "night/2026-09-02", "shlex try", SHLEX_CMD, SHLEX_PATHS, done=False, last_exit=1)
    landed = _item(RID_C, "gamma", "night/2026-09-01", SHLEX_TITLE, SHLEX_CMD, list(reversed(SHLEX_PATHS)))
    other_paths = _item(RID_C, "gamma", "night/2026-09-01", "shlex elsewhere", SHLEX_CMD, ["src/other.py"])
    other_cmd = _item(RID_C, "gamma", "night/2026-09-01", SHLEX_TITLE, "pytest tests/test_other.py -q", SHLEX_PATHS)
    forum = _forum([own, other_paths, other_cmd, tried, landed], [])
    assert forum_match(own, forum, exclude_repo_id=RID_A) is landed
    forum = _forum([own, tried], [])
    assert forum_match(own, forum, exclude_repo_id=RID_A) is tried
    # own-repo rows are never an origin, even when done
    assert forum_match(own, _forum([dict(own, done=True)], []), exclude_repo_id=RID_A) is None
    # never-attempted rows are not evidence
    assert forum_match(own, _forum([dict(tried, attempted=False)], []), exclude_repo_id=RID_A) is None
    assert forum_match(own, _forum([other_paths, other_cmd], []), exclude_repo_id=RID_A) is None
    # the path set is normalised: ./x, duplicates and order do not matter
    messy = dict(own, paths=["./" + SHLEX_PATHS[1], SHLEX_PATHS[0], SHLEX_PATHS[0]])
    assert forum_match(messy, _forum([tried], []), exclude_repo_id=RID_A) is tried
    assert forum_match(own, {}, exclude_repo_id=RID_A) is None


# --- (e) two widgets: exact-key consume attributes `applied`, never voids ----------------


def test_two_widgets_attribute_applied_reuse_without_void(tmp_path, mock_settings, ns_home, monkeypatch):
    seen: list[str] = []

    class Spy(Critic):
        def propose_brief(self, snapshot, size=2):
            seen.append(snapshot)
            return mock_upgrades_from_repo(self.repo, size=size)

    monkeypatch.setattr("nightshift.runner.Critic", Spy)
    repo_a = seed_widget(tmp_path / "alpha")
    repo_b = seed_widget(tmp_path / "beta")
    rid_a, rid_b = repo_id(repo_a), repo_id(repo_b)
    ra = run_night(repo_a, mock_settings, explicit=True)
    rb = run_night(repo_b, mock_settings, explicit=True)
    assert ra.halt_reason == rb.halt_reason == "remaining_zero"
    assert (rb.landed, rb.voided, rb.remaining_count) == (2, 0, 0)
    assert all(u.done and not u.void for u in rb.brief.upgrades)  # the forum never voids
    # A froze against an empty forum; B's critic saw A's landed rows at minute 0
    assert "## Portfolio forum" not in seen[0]
    assert "## Portfolio forum (other repos)" in seen[1]
    assert "- [done] alpha: Make test_add pass" in seen[1]
    assert "- [done] alpha: Make test_greet pass" in seen[1]
    assert FORUM_BLOCK_INSTRUCTION in seen[1]
    forum = load_forum(ns_home)
    events = forum["reuse_events"]
    assert len(events) == 2
    a_items = {i["id"]: i for i in forum["items"] if i["repo_id"] == rid_a}
    b_items = {i["id"]: i for i in forum["items"] if i["repo_id"] == rid_b}
    assert len(a_items) == len(b_items) == 2
    for ev in events:
        assert ev["kind"] == "applied" and ev["match"] == "check_hash+paths"
        assert ev["origin_repo_id"] == rid_a and ev["origin_repo_name"] == "alpha"
        assert ev["consumer_repo_id"] == rid_b and ev["consumer_repo_name"] == "beta"
        assert ev["consumer_night"] == rb.branch
        assert ev["origin_item_id"] in a_items and ev["consumer_item_id"] in b_items
        assert ev["origin_item_id"] != ev["consumer_item_id"]
        # same check_hash + pathset behind two repo-scoped ids
        assert ev["origin_item_id"].split("-", 2)[2] == ev["consumer_item_id"].split("-", 2)[2]
        assert ev["id"] == reuse_event_id(
            ev["origin_item_id"], ev["consumer_item_id"], ev["consumer_night"], "applied"
        )
        assert ev["id"].startswith("r-") and len(ev["id"]) == 14 and ev["at"]
    assert {ev["consumer_item_id"] for ev in events} == set(b_items)
    md = (ns_home / "forum.md").read_text(encoding="utf-8")
    assert "## Reuse\n- applied  alpha -> beta" in md
    # re-publishing B is idempotent: same events, same ids
    ids = sorted(ev["id"] for ev in events)
    again = publish_night(home=ns_home, report=rb, ledger=load_ledger(repo_b, home=ns_home))
    assert sorted(ev["id"] for ev in again["reuse_events"]) == ids
    # re-publishing A after B never attributes backwards: B ended after A started
    again = publish_night(home=ns_home, report=ra, ledger=load_ledger(repo_a, home=ns_home))
    assert sorted(ev["id"] for ev in again["reuse_events"]) == ids
    # a morning ingest neither invents nor drops events
    again = ingest_forum(ns_home, [repo_a, repo_b])
    assert sorted(ev["id"] for ev in again["reuse_events"]) == ids
    assert again["errors"] == []


# --- (f) a never-attempted consumer yields exactly one `proposed` event ------------------


def test_unattempted_consumer_yields_one_proposed_event(fixture_repo, ns_home):
    shlex, shlex_night = _shlex_forum()
    save_forum(ns_home, _forum([shlex], [shlex_night]))
    rid = repo_id(fixture_repo)
    other_cmd = "pytest tests/test_z.py -q"
    brief = Brief.freeze(
        [
            Upgrade(1, SHLEX_TITLE, SHLEX_CMD, list(SHLEX_PATHS)),
            Upgrade(2, "unrelated", other_cmd, ["z.py"]),
        ],
        branch="night/2026-09-04",
    )
    report = NightReport(
        repo=fixture_repo,
        branch="night/2026-09-04",
        main_ref="main",
        main_sha="",
        remaining_count=2,
        halt_reason="max_turns",
        brief=brief,
        started_at="2026-09-04T01:00:00",
        ended_at="2026-09-04T03:00:00",
    )
    forum = publish_night(home=ns_home, report=report, ledger={"entries": []})
    consumer_id = item_id(rid, check_hash(SHLEX_CMD), SHLEX_PATHS)
    assert [i["attempted"] for i in forum["items"] if i["repo_id"] == rid] == [False, False]
    assert len(forum["reuse_events"]) == 1
    ev = forum["reuse_events"][0]
    assert ev == {
        "id": reuse_event_id(shlex["id"], consumer_id, "night/2026-09-04", "proposed"),
        "at": ev["at"],
        "kind": "proposed",
        "origin_repo_id": RID_A,
        "origin_repo_name": "nightshift",
        "origin_item_id": shlex["id"],
        "consumer_repo_id": rid,
        "consumer_repo_name": fixture_repo.name,
        "consumer_night": "night/2026-09-04",
        "consumer_item_id": consumer_id,
        "match": "check_hash+paths",
    }
    assert ev["at"]
    assert len(publish_night(home=ns_home, report=report, ledger={"entries": []})["reuse_events"]) == 1
    # path (3) computes nothing
    forum = publish_error_stub(home=ns_home, repo=fixture_repo, error="critic down")
    assert len(forum["reuse_events"]) == 1 and len(forum["errors"]) == 1


def test_reuse_events_for_night_kinds_and_guards():
    shlex, shlex_night = _shlex_forum()
    forum = _forum([shlex], [shlex_night])
    night = "night/2026-09-04"

    def consumer(**over) -> dict:
        state = {"attempted": False, "done": False, **over}
        return _item(RID_B, "beta", night, SHLEX_TITLE, SHLEX_CMD, SHLEX_PATHS, **state)

    def events(item: dict, **kw) -> list[dict]:
        return reuse_events_for_night(
            forum, repo_id_=RID_B, repo_name="beta", night=night, items=[item], **kw
        )

    assert [e["kind"] for e in events(consumer())] == ["proposed"]
    assert [e["kind"] for e in events(consumer(attempted=True))] == ["attempted"]
    assert [e["kind"] for e in events(consumer(attempted=True, done=True))] == ["applied"]
    # void rows, older-night rows (kept done rows re-projected tonight) and empty nights never count
    assert events(consumer(voided=True, void_reason="duplicate_of_history")) == []
    assert events(dict(consumer(attempted=True, done=True), night="night/2026-09-01")) == []
    assert reuse_events_for_night(forum, repo_id_=RID_B, repo_name="beta", night="", items=[consumer()]) == []
    # an origin whose night ended after this night started is not one it could have read
    assert events(consumer(), started_at="2026-09-01T02:00:00") == []
    assert [e["kind"] for e in events(consumer(), started_at="2026-09-01T04:10:00")] == ["proposed"]
    assert [e["kind"] for e in events(consumer(), started_at="2026-09-04T01:00:00")] == ["proposed"]
    # unknown stamps never block (ingested origins have no ended_at)
    ingested = _forum([shlex], [dict(shlex_night, ended_at="")])
    assert len(
        reuse_events_for_night(
            ingested, repo_id_=RID_B, repo_name="beta", night=night, items=[consumer()], started_at="2026-09-01T02:00:00"
        )
    ) == 1
    # origin and consumer ids must be distinct
    assert events(dict(consumer(), id=shlex["id"])) == []
    # the origin's own repo never consumes from itself
    assert reuse_events_for_night(forum, repo_id_=RID_A, repo_name="nightshift", night=night, items=[dict(consumer(), repo_id=RID_A)]) == []
