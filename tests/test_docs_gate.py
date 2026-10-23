"""ADR 0007: the docs gate is job 1 when ADRs or the assumptions register are missing."""

from __future__ import annotations

import subprocess
from pathlib import Path

from nightshift import docs_gate
from nightshift.graph import path_allowed
from nightshift.host import run_check
from nightshift.runner import dry_run_brief

ADR_OK = """# ADR 001: Keep state in SQLite

- Status: Accepted (retrospective)
- Date: 2026-08-20
- Decision owners: maintainers

## Context

The tool needs a local audit store.

## Decision

Use SQLite. Rationale: single-file deploy (inferred).

## Consequences

One writer at a time.

## Evidence

- `store.py`, commit abc1234
"""

INDEX = "# ADRs\n\n| ADR | Decision |\n|---|---|\n| [001](001-sqlite.md) | SQLite |\n"
REGISTER = (
    "# Assumptions register\n\n"
    "| ID | Assumption | Evidence | Stated/Inferred | Impact if wrong | Review owner |\n"
    "|---|---|---|---|---|---|\n"
    "| A1 | One user per clone | README | Stated | locking | Nic |\n"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _documented(repo: Path) -> None:
    _write(repo, "docs/adr/001-sqlite.md", ADR_OK)
    _write(repo, "docs/adr/README.md", INDEX)
    _write(repo, "docs/assumptions.md", REGISTER)


def test_bare_repo_has_both_gaps(fixture_repo):
    result = docs_gate.audit(fixture_repo)
    assert not result.ok
    assert any("no ADR folder" in p for p in result.problems)
    assert any("assumptions register" in p for p in result.problems)
    assert docs_gate.main([str(fixture_repo)]) == 1


def test_documented_repo_passes(fixture_repo):
    _documented(fixture_repo)
    result = docs_gate.audit(fixture_repo)
    assert result.ok, result.problems
    assert result.adr_dir == "docs/adr"
    assert result.adrs == ["docs/adr/001-sqlite.md"]
    assert docs_gate.gate_upgrade(fixture_repo) is None


def test_house_style_adrs_are_not_a_gap(fixture_repo):
    # Porter Forces keeps ADRs in docs/technical/adr; spark-serve has no Context heading.
    _write(fixture_repo, "docs/technical/adr/001-x.md", "# ADR 001: X\n\nWe chose X.\n")
    _write(fixture_repo, "docs/assumptions.md", REGISTER)
    result = docs_gate.audit(fixture_repo)
    assert result.ok, result.problems
    assert result.adr_dir == "docs/technical/adr"
    assert result.advisories  # noted, never rewritten


def test_empty_register_is_a_gap(fixture_repo):
    _documented(fixture_repo)
    _write(fixture_repo, "docs/assumptions.md", "# Assumptions\n\nTBD\n")
    result = docs_gate.audit(fixture_repo)
    assert result.problems == ["docs/assumptions.md has no entries"]


def test_gate_upgrade_targets_existing_folder(fixture_repo):
    _write(fixture_repo, "docs/technical/adr/001-x.md", ADR_OK)
    gate = docs_gate.gate_upgrade(fixture_repo, "abc123")
    assert gate is not None and docs_gate.is_gate(gate)
    assert gate.paths == ["docs/technical/adr/", "docs/assumptions.md"]
    assert "--base abc123" in gate.check_command
    assert "Match the format already used in docs/technical/adr/" in gate.note
    assert path_allowed("docs/technical/adr/002-new.md", set(gate.paths))
    assert not path_allowed("src/app.py", set(gate.paths))


def test_check_holds_new_adrs_to_the_template(fixture_repo):
    base = _git(fixture_repo, "rev-parse", "HEAD")
    gate = docs_gate.gate_upgrade(fixture_repo, base)
    assert gate is not None
    assert not run_check(fixture_repo, gate, timeout=60).ok

    _write(fixture_repo, "docs/adr/001-sqlite.md", "# ADR 001: SQLite\n\nStatus: Accepted\n")
    _write(fixture_repo, "docs/assumptions.md", REGISTER)
    res = run_check(fixture_repo, gate, timeout=60)
    assert not res.ok
    assert "lacks Context, Decision, Evidence" in res.output
    assert "not linked" in res.output

    _documented(fixture_repo)
    _commit(fixture_repo, "night: docs")
    res = run_check(fixture_repo, gate, timeout=60)
    assert res.ok, res.output
    assert "docs gate OK" in res.output


def test_dry_run_puts_gate_first(fixture_repo, mock_settings):
    mock_settings.docs_gate = True
    brief = dry_run_brief(fixture_repo, mock_settings)
    assert len(brief.upgrades) == 3  # JOBS 2 + the gate
    first = brief.upgrades[0]
    assert first.id == 1 and docs_gate.is_gate(first)
    assert first.paths == ["docs/adr/", "docs/assumptions.md"]
    assert not any(docs_gate.is_gate(u) for u in brief.upgrades[1:])


def test_gate_respects_brief_max(fixture_repo, mock_settings):
    mock_settings.docs_gate = True
    mock_settings.brief_size = 5
    brief = dry_run_brief(fixture_repo, mock_settings)
    assert len(brief.upgrades) == 5
    assert docs_gate.is_gate(brief.upgrades[0])


def test_no_gate_when_documented_or_off(fixture_repo, mock_settings):
    brief = dry_run_brief(fixture_repo, mock_settings)  # conftest: gate off
    assert not any(docs_gate.is_gate(u) for u in brief.upgrades)
    _documented(fixture_repo)
    _commit(fixture_repo, "docs")
    mock_settings.docs_gate = True
    brief = dry_run_brief(fixture_repo, mock_settings)
    assert len(brief.upgrades) == 2
    assert not any(docs_gate.is_gate(u) for u in brief.upgrades)


def test_gate_is_not_voided_by_history(fixture_repo, mock_settings, monkeypatch):
    monkeypatch.setattr(
        "nightshift.runner.history_void_reason", lambda u, ledger: "failed_before:night/x"
    )
    mock_settings.docs_gate = True
    brief = dry_run_brief(fixture_repo, mock_settings)
    gate = brief.upgrades[0]
    assert docs_gate.is_gate(gate) and not gate.void
    assert all(u.void for u in brief.upgrades[1:])


def test_env_toggle(monkeypatch):
    from nightshift.config import Settings

    monkeypatch.setenv("NIGHTSHIFT_DOCS_GATE", "1")
    assert Settings().docs_gate is True
    monkeypatch.setenv("NIGHTSHIFT_DOCS_GATE", "off")
    assert Settings().docs_gate is False


def test_docs_folder_keeps_its_spelling(fixture_repo):
    # face-research-lab keeps DOCs/adrs; job paths must match what git reports.
    _write(fixture_repo, "DOCs/adrs/001-x.md", ADR_OK)
    gate = docs_gate.gate_upgrade(fixture_repo)
    assert gate is not None
    assert gate.paths == ["DOCs/adrs/", "DOCs/assumptions.md"]
