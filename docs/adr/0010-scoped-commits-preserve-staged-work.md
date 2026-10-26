# ADR 0010: Scoped commits preserve staged work

Status: Accepted
Date: 2026-10-24

## Context
Nightshift runs against operator clones that may already have staged work. A normal `git commit` could absorb that staged content into a night branch and destroy the operator's intent. The architecture safety section says scoped commits use `git commit --only` so staged work survives.

## Decision
Nightshift commits only the paths it changed for the current job by using `git commit --only`. It never force-pushes, never commits to `main` or `master`, and leaves unrelated staged work untouched.

## Consequences
- Operator WIP survives an overnight run.
- Night branches contain only Nightshift-authored changes.
- Git safety regressions must be tested because a wrong commit mode can corrupt the review contract.

## Evidence
- `docs/architecture.md` — Safety short section and `git commit --only`.
- `src/nightshift/gitops.py` — branch, commit, and revert helpers.
- `tests/test_git_safety_regressions.py` — git safety behavior.
- commit `ab38424` — fix preserve staged work and enforce literal file boundaries.

## Rationale
- Stated: scoped commits use `git commit --only` so staged WIP survives.
- (inferred) Preserving operator staging is required because Nightshift is a guest in a live portfolio checkout, not a disposable sandbox.
