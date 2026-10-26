# ADR 0008: Host checks are local subprocesses and fail closed

Status: Accepted
Date: 2026-10-24

## Context
Nightshift must decide whether a frozen upgrade is done without asking an LLM to grade its own work. The README and design contract say host pytest is truth. `src/nightshift/host.py` runs checks as local subprocesses, and `docs/architecture.md` states host checks are not an LLM. A malformed or empty check command can raise before the subprocess runs; if that exception escapes, the whole night halts with `error` instead of failing one job.

## Decision
Treat the host check as the only authority for `done`. Run it in a local subprocess with a timeout. If the command is empty, malformed, or cannot be parsed, return a failing check result rather than raising into the runner. A job is marked done only when its current check passes and its declared `paths[]` changed during the night.

## Consequences
- A bad critic-supplied check cannot silently pass or kill the entire night; it fails the job and can be voided after repeated host failures.
- The runner remains deterministic and auditable because pass or fail comes from exit status, not model opinion.
- Check commands must be simple enough for the host parser and complex shell checks are accepted only when the host can execute them safely.

## Evidence
- `docs/design.md` — Host pytest is truth.
- `docs/architecture.md` — Host checks are local subprocesses not an LLM.
- `src/nightshift/host.py` — check execution and interpreter handling.
- `tests/test_host_check.py` — host check behavior.
- commit `96a87b9` — fix keep live overnight state and host truth after failures.

## Rationale
- Stated: host pytest is truth and host checks are local subprocesses.
- (inferred) Failing closed on malformed checks preserves the overnight contract that one bad job should not destroy the whole night.
