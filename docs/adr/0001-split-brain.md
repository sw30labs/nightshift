# 0001. Split brain

**Accepted** · 2026-09

## Context

One box cannot hold both the writer and the critic. The writer is Spark DS4 vLLM. The critic is Mac oMLX. Electricity is cheaper at night; both machines are already paid for.

## Decision

Two roles, two endpoints, never the same server.

- **Writer** edits files inside the target. No network from the writer. Model id `auto` = first id from `/v1/models`.
- **Critic** inspects, scores, slashes, reverts, halts. **No write tool on that class.**
- OpenAI-compat `POST /chat/completions`. oMLX Bearer `test`. Tests use `--mock`.

## Consequences

Parallel nights would interleave jobs on one Spark and one oMLX load. Bags are a queue (0005). Pointing both env vars at one URL is a misconfig, not a mode.
