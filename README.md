# Nightshift

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2-1C3C3C)](https://github.com/langchain-ai/langgraph)
[![LoopScope](https://img.shields.io/badge/LoopScope-hook-7a8f62)](https://github.com/sw30labs/loopscope)
[![License](https://img.shields.io/badge/License-Apache_2.0-green.svg)](LICENSE)

A local overnight coding agent. Pick an existing git project, press **Run**, go to sleep. Two heterogeneous local LLMs run a Ralph loop against a **frozen three-item brief** until `remaining_count` hits zero or the clock halt (default 06:00 local). Morning: a `night/YYYY-MM-DD` branch, a [LoopScope](https://github.com/sw30labs/loopscope) replay, and `.nightshift/summary.md`.

This is not a chatbot. Not two models chatting. The product is a branch you can `git diff`.

Personal-capacity OSS. GitHub is the remote. Morning review is VS Code, not Cursor. Author: Nicolas Cravino / sw30labs.

## The two brains

Different physics. Do not point both at the same server.

| Role | Machine | Default endpoint | Model | Tools |
|---|---|---|---|---|
| **Writer** | Spark DS4 only | `http://192.168.86.44:8000/v1` | `deepseek-v4-flash` | edit/create files inside the target repo. No network from the writer. |
| **Critic** | Mac oMLX only | `http://127.0.0.1:8000/v1` | `GLM-5.3-Flash-MLX-8bit` | inspect, score, slash, revert, halt. **Never writes the project body.** |

OpenAI-compatible `POST /chat/completions`. Mac oMLX requires `Authorization: Bearer test`. Spark DS4 usually ignores the header.

```bash
export NIGHTSHIFT_WRITER_BASE_URL=http://192.168.86.44:8000/v1
export NIGHTSHIFT_WRITER_MODEL=deepseek-v4-flash
export NIGHTSHIFT_CRITIC_BASE_URL=http://127.0.0.1:8000/v1
export NIGHTSHIFT_CRITIC_MODEL=GLM-5.3-Flash-MLX-8bit
export NIGHTSHIFT_API_KEY=test           # oMLX; Spark usually ignores Authorization
export NIGHTSHIFT_ROOTS=$HOME/REPOS      # default
export NIGHTSHIFT_HALT_AT=06:00          # local clock
```

The cloud CI VM has neither oMLX nor the Sparks. Tests use `--mock` / `NIGHTSHIFT_MOCK=1`. Do not require live GPUs in CI.

## Install

From a source checkout, same shape as LoopScope:

```bash
./setup_and_run.sh          # venv + tests + mock deck over ~/REPOS
./setup_and_run.sh --live   # oMLX + spark-serve ds4
./setup_and_run.sh --demo   # seeded widget only
```

`--setup-only` stops after deps and tests. `--live` fills the two-brain URLs if they are unset. The deck binds `127.0.0.1:43171` by default (`NIGHTSHIFT_PORT` / `--port`); LoopScope stays on `:7788`.

Python 3.11+. By hand:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

LoopScope is optional but preferred. Same venv:

```bash
pip install git+https://github.com/sw30labs/loopscope.git
```

If LoopScope is missing, Nightshift still runs: JSONL at `.nightshift/events.jsonl` plus a tiny stdlib page on `:7788`.

LangGraph is a hard dependency (the cycle graph). A plain state-machine fallback is used only if the import fails.

## Command deck

Stdlib HTTP. No React, no Vue, no Next, no Tailwind-as-a-framework.

```bash
nightshift serve --mock --demo          # VM / laptop without GPUs
nightshift serve                        # live Mac: oMLX + spark-serve ds4
```

Defaults to `http://127.0.0.1:43171`. Lists git repos under `NIGHTSHIFT_ROOTS` (default `~/REPOS`; skip `DEPRECATED` unless toggled). Click one, press **Run**. Live `remaining_count`, which brain is hot, last host-check output, LoopScope link, then `summary.md` after halt.

`--demo` seeds a failing `widget` repo so the list is not empty.

## CLI (equally first-class)

```bash
nightshift list
nightshift run /path/to/repo
nightshift run /path/to/repo --mock
nightshift status
nightshift serve --host 127.0.0.1 --port 43171
```

`--push` is off by default. Nightshift never force-pushes, never amends, never deletes your branches, never commits to `main`/`master`.

## Overnight contract

Minute 0, critic only (no writer, no project-body writes):

1. Create and checkout `night/YYYY-MM-DD` (append `-HHMM` if that name exists).
2. Read tree, tests, README, recent git log.
3. Emit a **frozen brief**: exactly three upgrades. Each must be checkable by a host command (`pytest`, a script, file-exists + content grep, `npm test`, …). Not “cleaner architecture.” If the repo has no tests, one of the three may be “add a smoke test that fails then make it pass.”
4. Persist `.nightshift/brief.json` on the night branch. After freeze the writer cannot add a fourth upgrade. The critic cannot quietly expand scope at 3am.

Then Ralph until `remaining_count == 0` or clock halt:

1. Critic writes a one-line job from remaining brief items.
2. Writer edits files on the night branch.
3. **Host** runs the real check commands. Pytest/output is truth, not the model’s opinion.
4. Critic reads diff + check logs, decrements items that actually pass, **reverts** gold-plating / files outside the brief (`git checkout --` on unapproved paths; new untracked files are unlinked).
5. LoopScope shows writer vs critic as two colours; `remaining_count` is the plot.

Morning artifacts on the night branch:

- `.nightshift/brief.json`
- `.nightshift/summary.md` (what changed, what was refused, remaining if the clock halted)
- small real commits as the night proceeds

## LoopScope

One hook. Nightshift does not fork LoopScope and does not wrap graph nodes.

```python
import loopscope
scope = loopscope.start(open_browser=True)
config = loopscope.attach(app)
# invoke / astream
loopscope.finish(config)
scope.hold()
```

Dashboard default: `http://127.0.0.1:7788`. Overnight replay:

```bash
python -m loopscope.replay path/to/repo/.nightshift/events.jsonl --speed 8
```

## Mock mode

```bash
nightshift run ./some-repo --mock --no-observe
NIGHTSHIFT_MOCK=1 nightshift serve --demo
pytest
```

The mock provider implements the same chat shape as the live HTTP clients. Unit and integration tests stay green with no Spark and no oMLX.

## Safety

- Target must be a git work tree. Always branch first; never commit to `main`/`master`.
- Refuse `/` and `$HOME`. Refuse Nightshift’s own repo unless you explicitly selected it.
- Cap turns (default 20) and wall clock (halt-at).
- Writer tools: edit/create inside the target only. Critic has no write tool.
- Host checks run in the target repo with a timeout.
- No `git push` unless `--push`.

## Tests

```bash
pytest
```

Covered: brief freeze (no fourth upgrade), critic cannot write files, host-check truth, branch naming, revert of unapproved paths, mock end-to-end to `remaining_count` 0 on a fixture git repo (failing tests → patches → real pytest → `summary.md` → night branch exists, `main` unchanged), command deck list + Run.

## Layout

```
src/nightshift/
  cli.py         list / run / status / serve
  deck.py        stdlib HTTP command deck
  runner.py      overnight contract
  graph.py       LangGraph cycle + snapshot / revert
  llm.py         OpenAI-compat clients, mock provider, writer, critic
  host.py        real check commands
  gitops.py      branch, commit, revert (never force)
  observe.py     LoopScope hook + JSONL fallback
```

Apache-2.0. Copyright 2026 Nicolas Cravino.
