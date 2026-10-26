# ADR 0016: Halt is cooperative after the current turn

Status: Accepted
Date: 2026-10-24

## Context
Nightshift runs unattended and can be halted by the deck, CLI, clock, or operator request. A hard kill could leave half-written files, an inconsistent status board, or a bag lock that looks live. The design says this is not a hard kill and halt finishes the current turn.

## Decision
Halt is cooperative. A halt request is consumed by the runner after the current turn. Bag halt marks remaining queued targets skipped rather than killing the process mid-write.

## Consequences
- The filesystem and status board remain recoverable.
- Operators may wait one turn for halt to take effect.
- Tests must cover halt request consumption and bag skip behavior.

## Evidence
- `docs/design.md` — Not a hard kill and halt finishes the current turn.
- `src/nightshift/status.py` — halt request and live board behavior.
- `src/nightshift/bag.py` — bag halt and skip rest logic.
- `tests/test_halt_request.py` — halt request behavior.
- commit `e42b50a` — fix harden the command deck against stale polls and unsafe requests.

## Rationale
- Stated: halt is not a hard kill.
- (inferred) Cooperative halt is required because the writer and host can mutate git state during a turn.
