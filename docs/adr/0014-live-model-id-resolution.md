# ADR 0014: Model IDs are resolved from live /v1/models

Status: Accepted
Date: 2026-10-24

## Context
Local model servers can expose different model ids over time. A hard-coded writer model can make a night fail before any useful work happens. The config default moved to `auto` for the writer, and the commit history records resolving model ids from live `/v1/models`.

## Decision
When a model id is `auto`, Nightshift queries the server `/v1/models` endpoint and resolves a usable model id before the writer or critic call. The default writer model is `auto`.

## Consequences
- The overnight loop adapts to the local server inventory.
- Model discovery failures are visible before the Ralph loop burns turns.
- Operators can still pin a model explicitly when needed.

## Evidence
- `src/nightshift/config.py` — writer model default `auto`.
- `src/nightshift/llm.py` — OpenAI-compatible client and model resolution.
- `tests/test_resolve_model.py` — live model resolution behavior.
- commit `d35dc69` — writer critic resolve model id from live /v1/models default writer=auto.
- commit `0087ed8` — fix config point writer default at sparkone.local.

## Rationale
- Stated: model id is resolved from live `/v1/models` and writer default is `auto`.
- (inferred) Local estate hardware changes often enough that runtime discovery is safer than a frozen model string.
