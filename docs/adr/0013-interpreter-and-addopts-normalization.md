# ADR 0013: Interpreter and pytest addopts are normalized

Status: Accepted
Date: 2026-10-24

## Context
Host checks run inside target clones that may use different Python interpreters and may carry CI-oriented pytest addopts such as coverage flags. If Nightshift uses the wrong interpreter or leaves repo addopts in place, checks can fail for reasons unrelated to the current job.

## Decision
The host resolves the target interpreter before running checks. For pytest checks, Nightshift strips CI addopts by inserting `-o addopts=` after each pytest invocation, including shell-form commands where practical, so the check runs the requested test selection without inherited coverage or plugin noise.

## Consequences
- Checks are closer to the command the critic intended.
- Repo-level CI configuration cannot silently make every overnight job fail.
- Host parsing must handle both exec and shell forms carefully.

## Evidence
- `docs/architecture.md` — host module job is real checks, interpreter, strip CI addopts.
- `src/nightshift/host.py` — interpreter resolution and check execution.
- `tests/test_interpreter.py` — interpreter selection behavior.
- `tests/test_host_pytest_addopts.py` — addopts stripping behavior.
- commit `96a87b9` — fix keep live overnight state and host truth after failures.

## Rationale
- Stated: host strips CI addopts and resolves the interpreter.
- (inferred) Normalizing the check environment is required because host pytest is the only truth for job completion.
