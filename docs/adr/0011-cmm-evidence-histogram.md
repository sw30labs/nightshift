# ADR 0011: CMM is an evidence histogram not a score

Status: Accepted
Date: 2026-10-24

## Context
The portfolio needs a way to see which repos have real overnight evidence. The README and design explicitly reject invented maturity scores. CMM must be derived from forum rows and merge evidence, not from an LLM opinion or a default branch heuristic.

## Decision
CMM is a pure function over `forum.json`. Levels L1 through L4 use forum evidence only. L5 requires a Nightshift checkout plus merge evidence for a done item. Each repo is counted once at its highest satisfied level. An empty forum is all L0.

## Consequences
- The histogram can be regenerated and audited.
- Empty columns are meaningful and are not filled with guesses.
- CMM cannot be used as a task ranking; it is evidence density.

## Evidence
- `README.md` — CMM atlas is empty until evidence and no invented scores.
- `docs/design.md` — Not invented maturity scores.
- `src/nightshift/cmm.py` — histogram, scoring, and rendering.
- `tests/test_cmm.py` — evidence rules and level behavior.
- commit `293f6a9` — docs add design architecture and six ADRs.

## Rationale
- Stated: CMM is evidence and not a score.
- (inferred) Keeping CMM deterministic avoids turning a portfolio review into a model hallucination contest.
