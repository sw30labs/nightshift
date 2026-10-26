# ADR 0012: Portfolio priorities are opt-in and bounded

Status: Accepted
Date: 2026-10-24

## Context
Nightshift can run against a portfolio, but owner strategy must not be silently rewritten by a nightly loop. The portfolio priorities document defines an opt-in contract that reads the Porter-backed JSON source, validates it, and selects a bounded bag.

## Decision
Portfolio execution is enabled only by `~/.nightshift/portfolio.json`. Selection fetches and validates the current source, archives the digest, and refuses fallback to recency or cached authority. The initial contract is one target with two checkable jobs, sequential execution, and no automatic meta slot. The supplied configuration preserves the org-refactor pause.

## Consequences
- Nightshift competes under owner allocation instead of inventing strategy.
- Source failures stop selection and are visible as priority errors.
- Morning Prayers remains the human-on-the-loop surface for accept, reject, or defer.

## Evidence
- `docs/portfolio-priorities.md` — purpose, selection contract, pause, and cadence.
- `src/nightshift/priorities.py` — policy, refresh, choose, attempt, and review helpers.
- `src/nightshift/bag.py` — portfolio selection path and one-target two-job clamp.
- `tests/test_portfolio_priorities.py` — priority selection and pause behavior.
- commit `cf52a79` — Merge pull request feat portfolio priorities.
- commit `1d6fa8a` — Drive nightly bags from owner portfolio priorities.

## Rationale
- Stated: owner sets direction and Nightshift turns current priorities into a bounded nightly bag.
- (inferred) Keeping the first integration paused and narrow limits review burden while the portfolio contract is validated.
