from __future__ import annotations

import json

import pytest

from nightshift.forum import load_forum
from nightshift.gitops import current_branch, git, rev_parse
from nightshift.graph import LoopNodes, NightContext, night_changed_rels, read_snapshot
from nightshift.llm import (
    MAX_FULL_FILE_CHARS,
    Critic,
    MockChatClient,
    OpenAICompatClient,
    Writer,
    critic_brief_system,
)
from nightshift.models import Brief, CheckResult, SafetyError, Upgrade, WriterResult
from nightshift.runner import freeze_brief, run_night
from nightshift.status import StatusBoard


def _brief(paths=None):
    return Brief.freeze([
        Upgrade(1, "first", "true", paths or ["widget.py"]),
        Upgrade(2, "second", "true", ["README.md"]),
    ])


def _context(repo, settings):
    return NightContext(
        repo=repo,
        settings=settings,
        writer=Writer(MockChatClient("writer", repo), repo),
        critic=Critic(MockChatClient("critic", repo), repo),
        status=StatusBoard(settings.state_dir()),
        clock=settings.now_fn,
        deadline=settings.now_fn(),
    )


class PayloadClient:
    def __init__(self, payload):
        self.payload = payload

    def chat(self, messages, **kwargs):
        return json.dumps(self.payload)


@pytest.mark.parametrize("size", [2, 3, 4, 5])
def test_frozen_brief_prompt_contains_valid_json_example(size):
    prompt = critic_brief_system(size)
    example = prompt.split("Return JSON only:\n", 1)[1].split("\nExactly", 1)[0]
    assert len(json.loads(example)["upgrades"]) == size


@pytest.mark.parametrize("halt, expected", [(False, False), ("false", False), ("0", False), (True, True), ("true", True)])
def test_critic_halt_does_not_treat_false_string_as_true(fixture_repo, halt, expected):
    critic = Critic(PayloadClient({"halt": halt}), fixture_repo)
    assert critic.opinion(_brief(), "", "")["halt"] is expected


def test_critic_ignores_nonfinite_or_fractional_ids(fixture_repo):
    critic = Critic(PayloadClient({"passed_ids": [float("inf"), float("nan"), 1.5, 2]}), fixture_repo)
    assert critic.opinion(_brief(), "", "")["passed_ids"] == [2]


def test_malformed_http_json_is_a_retryable_writer_failure(fixture_repo, monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"<html>temporary backend failure</html>"

    monkeypatch.setattr("nightshift.llm.urllib.request.urlopen", lambda *a, **k: Response())
    client = OpenAICompatClient("http://localhost:1/v1", "test")
    result = Writer(client, fixture_repo).apply_job("fix", _brief(), "")
    assert result.written == []
    assert any("invalid JSON response" in note and "retry" in note for note in result.refused)


def test_short_full_file_reply_cannot_truncate_large_existing_file(fixture_repo):
    path = fixture_repo / "README.md"
    original = "# Existing documentation\n" + "content\n" * MAX_FULL_FILE_CHARS
    path.write_text(original)
    client = PayloadClient({"files": [{"path": "README.md", "content": "# Short fragment\n"}]})
    result = Writer(client, fixture_repo).apply_job("edit", _brief(["README.md"]), "")
    assert result.written == []
    assert any("existing file" in note and "patches[]" in note for note in result.refused)
    assert path.read_text() == original


def test_snapshot_ignores_symlinks_to_external_files_and_secrets(fixture_repo, tmp_path):
    outside = tmp_path / "private.txt"
    outside.write_text("EXTERNAL_PRIVATE_CONTENT")
    (fixture_repo / ".env").write_text("LOCAL_SECRET_CONTENT")
    (fixture_repo / "alias.py").symlink_to(fixture_repo / ".env")
    (fixture_repo / "README.md").unlink()
    (fixture_repo / "README.md").symlink_to(outside)
    snapshot = read_snapshot(fixture_repo, focus=["alias.py", "README.md"])
    assert "EXTERNAL_PRIVATE_CONTENT" not in snapshot
    assert "LOCAL_SECRET_CONTENT" not in snapshot
    assert "## job file alias.py" not in snapshot


def test_changed_paths_preserve_unicode_and_embedded_newline(fixture_repo):
    base = rev_parse(fixture_repo, "HEAD")
    names = {"café.py", "two\nlines.py"}
    for name in names:
        (fixture_repo / name).write_text("value = 1\n")
        git(fixture_repo, "add", "--", name)
    git(fixture_repo, "commit", "-m", "filenames", extra_env={
        "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@localhost",
        "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@localhost",
    })
    assert names <= night_changed_rels(fixture_repo, base)


@pytest.mark.parametrize("job_path, dirty_path", [("src/", "src/user.py"), ("./src/user.py", "src/user.py"), ("src/user.py", "src/")])
def test_dirty_directory_overlap_voids_upgrade(fixture_repo, mock_settings, monkeypatch, job_path, dirty_path):
    ctx = _context(fixture_repo, mock_settings)
    ctx.preexisting = {dirty_path}
    monkeypatch.setattr(ctx.critic, "propose_brief", lambda *a, **k: _brief([job_path]).upgrades)
    brief = freeze_brief(ctx, "snapshot", "night/test")
    assert brief.upgrades[0].void_reason == "dirty_in_tree"
    assert not brief.upgrades[1].void


def test_writer_syntax_rollback_restores_head_instead_of_staged_bad_contents(fixture_repo, mock_settings):
    path = fixture_repo / "widget.py"
    original = path.read_text()
    path.write_text("def broken(:\n")
    git(fixture_repo, "add", "--", "widget.py")
    ctx = _context(fixture_repo, mock_settings)

    class AlreadyWritten:
        def apply_job(self, *a, **k):
            return WriterResult(written=["widget.py"], message="", raw="", refused=[])

    ctx.writer = AlreadyWritten()
    result = LoopNodes(ctx).writer({"brief": _brief().to_dict(), "job_upgrade_id": 1})
    assert result["compile_errors"]
    assert result["written"] == []
    assert path.read_text() == original
    assert git(fixture_repo, "diff", "--cached").stdout == ""


@pytest.mark.parametrize("regenerates_side_effect", [False, True])
def test_scoring_rechecks_host_truth_after_reverting_unapproved_files(
    fixture_repo, mock_settings, monkeypatch, regenerates_side_effect
):
    git(fixture_repo, "checkout", "-b", "night/recheck")
    ctx = _context(fixture_repo, mock_settings)
    ctx.base_sha = rev_parse(fixture_repo, "HEAD")
    source = fixture_repo / "widget.py"
    original = source.read_text()
    source.write_text("def add(a, b):\n    return a + b\n")
    (fixture_repo / "README.md").write_text("# Intended docs change\n")
    brief = _brief(["README.md"])
    checked = []

    def check_restored_tree(repo, upgrade, timeout):
        checked.append(upgrade.id)
        assert source.read_text() == original
        if regenerates_side_effect:
            (repo / "unapproved.txt").write_text("host side effect\n")
        return CheckResult(upgrade.id, upgrade.check_command, False, 1, "original check failed")

    monkeypatch.setattr("nightshift.graph.run_check", check_restored_tree)
    state = {
        "brief": brief.to_dict(), "job_upgrade_id": 1, "turn": 1,
        "check_results": [CheckResult(1, "true", True, 0, "green before revert").__dict__],
        "written": ["README.md"],
    }
    if regenerates_side_effect:
        with pytest.raises(SafetyError, match="cannot verify the final tree"):
            LoopNodes(ctx).critic_score(state)
        assert not (fixture_repo / "unapproved.txt").exists()
    else:
        result = LoopNodes(ctx).critic_score(state)
        assert not Brief.from_dict(result["brief"]).upgrades[0].done
        assert result["last_check"]["ok"] is False
        assert result["job_feedback"]["output"] == "original check failed"
        assert all(not row["ok"] for row in result["check_results"])
    assert checked == [1, 2]
    assert source.read_text() == original


@pytest.mark.parametrize("broken", ["make_clients", "freeze_snapshot", "build_cycle_app", "push_branch"])
def test_lifecycle_failures_release_runner_status(fixture_repo, mock_settings, monkeypatch, broken):
    def fail(*args, **kwargs):
        raise RuntimeError(f"{broken} failed")

    mock_settings.push = broken == "push_branch"
    monkeypatch.setattr(f"nightshift.runner.{broken}", fail)
    with pytest.raises(RuntimeError, match=f"{broken} failed"):
        run_night(fixture_repo, mock_settings)
    board = StatusBoard(mock_settings.state_dir()).read()
    assert board.state == "error"
    assert board.runner_pid is None
    assert board.brain == ""
    assert f"{broken} failed" in board.error
    forum = load_forum(mock_settings.home)
    assert len(forum["nights"]) == 1
    if broken in {"make_clients", "freeze_snapshot"}:
        assert current_branch(fixture_repo) == "main"
    else:
        assert forum["nights"][0]["branch"] == current_branch(fixture_repo)
