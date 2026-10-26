# ADR 0009: Writer edits are locked to the current job paths

Status: Accepted
Date: 2026-10-24

## Context
The writer is a local model with file access but no network. The frozen brief gives each upgrade a `paths[]` list. Without a literal boundary, the writer could gold-plate, touch unrelated files, or expand scope after the critic freeze. The design contract says unapproved paths are reverted and the restored tree is rechecked before scoring.

## Decision
The writer may only patch files inside the current upgrade `paths[]`. Any write outside that set is reverted before host scoring. The current job can be marked done only if its declared paths changed during the night.

## Consequences
- The brief cannot grow through writer behavior.
- Reviewers see a diff scoped to the job being worked.
- Reverts and rechecks add host work but preserve the frozen contract.

## Evidence
- `docs/design.md` — Writer edits and critic never writes the project body.
- `README.md` — Writer cannot grow the brief.
- `src/nightshift/graph.py` — Ralph cycle, revert, and recheck flow.
- `tests/test_writer_path_lock.py` — path boundary behavior.
- commit `ab38424` — fix preserve staged work and enforce literal file boundaries.

## Rationale
- Stated: writer stays inside `paths[]` and unapproved paths are reverted.
- (inferred) Literal path boundaries are the cheapest way to keep a local writer from expanding a frozen overnight brief.
