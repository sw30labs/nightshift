# ADR 0015: Thinking effort and token budget fit CoT plus JSON

Status: Accepted
Date: 2026-10-24

## Context
The writer must emit structured JSON patches after reasoning. If the token budget is too small, chain-of-thought can consume the window and truncate the JSON. If it is too large, local servers may stall or exceed context. The commit history records a change to max thinking effort and a token budget that fits CoT plus JSON.

## Decision
Writer and critic calls use a thinking effort and token budget sized for both reasoning and the final structured response. The budget is treated as part of the LLM contract, not an incidental tuning knob.

## Consequences
- Fewer malformed writer responses caused by truncated JSON.
- Local model servers need enough context headroom for the configured effort.
- Tests must cover thinking effort behavior so regressions are visible.

## Evidence
- `src/nightshift/llm.py` — writer and critic request construction.
- `tests/test_thinking_effort.py` — thinking effort behavior.
- commit `2352b96` — writer critic max thinking effort and a token budget that fits CoT plus JSON.

## Rationale
- Stated: the token budget must fit CoT plus JSON.
- (inferred) Structured patch output is the product boundary, so reasoning budget and response budget must be negotiated together.
