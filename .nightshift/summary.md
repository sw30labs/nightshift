# Nightshift — 2026-10-24

**0 of 3 landed · 0 void · 3 open — crashed at turn 1: git add -- .nightshift/brief.json docs/adr/README.md docs/adr/0008-host-checks-fail-closed.md docs/adr/0009-writer-path-lock.md docs/adr/0010-scoped-commits-preserve-staged-work.md docs/adr/0011-cmm-evidence-histogram.md docs/adr/0012-portfolio-priorities-opt-in.md docs/adr/0013-interpreter-and-addopts-normalization.md docs/adr/0014-live-model-id-resolution.md docs/adr/0015-thinking-effort-token-budget.md docs/adr/0016-cooperative-halt.md docs/assumptions.md failed (1): The following paths are ignored by one of your .gitignore files:
.nightshift
hint: Use -f if you really want to add them.
hint: Disable this message with "git config set advice.addIgnoredFile false"**

**Repo:** `/Users/spider/REPOS/nightshift`
**Branch:** `night/2026-10-24`
**Halt:** error
**Remaining:** 3
**Base:** main @ bbe1b8f
**Main:** main @ bbe1b8f (unchanged)
**Host python:** /Users/spider/miniconda3/envs/nightshift/bin/python (conda env: dir name)

## Frozen brief

- **#1 [open]** ADR + assumptions gate: document the project's decisions and assumptions
  - check: `python /Users/spider/REPOS/nightshift/src/nightshift/docs_gate.py --check --base bbe1b8faa2d34c3eefb57a97ee10e855f41bddfd`
  - paths: docs/adr/, docs/assumptions.md
  - note: Non-negotiable, runs first. Read the COMPLETE project in the snapshot (code, tests, config, README, docs, git log) before writing. Write retrospective ADRs in docs/adr/: one file per real, load-bearing decision visible in code, config, commits or existing docs. Never invent a decision. Any rationale not stated in code, docs or commits is labelled '(inferred)'. Each ADR cites its evidence (file paths, commit hashes). Match the format already used in docs/adr/. Keep docs/adr/README.md as an index table linking every ADR. Write docs/assumptions.md as an assumptions register: a table with ID, Assumption, Evidence, Stated/Inferred, Impact if wrong, Review owner. Gaps at freeze: no assumptions register under docs/ (e.g. docs/assumptions.md)
- **#2 [open]** Add a host-check regression test for empty and unbalanced-quote check commands
  - check: `python -m pytest tests/test_host_check_malformed.py -q`
  - paths: tests/test_host_check_malformed.py, src/nightshift/host.py
- **#3 [open]** Add a setup script smoke test for bash syntax and --help exit status
  - check: `python -m pytest tests/test_setup_help.py -q`
  - paths: tests/test_setup_help.py, setup_and_run.sh

## Voided / skipped-as-duplicate

- none

## What changed

```
7a1e762 nightshift: freeze brief (3 upgrades)
```

```
(no diff)
```

`git diff bbe1b8f...night/2026-10-24 -- . ':!.nightshift'`

## Land it

- merge: `git checkout main && git merge --no-ff night/2026-10-24`
- drop: `git branch -D night/2026-10-24`
- `.nightshift/ledger.json` rides along on merge

## What the critic refused

- Upgrade 1 host check passed: docs gate OK with 16 ADRs and assumptions register.
- All changes are within current job paths: docs/adr/ and docs/assumptions.md.
- No outside project files were written, so no revert required.
- Upgrades 2 and 3 remain unpassed because their required test files are absent.

## Remaining

- **#1** ADR + assumptions gate: document the project's decisions and assumptions — `python /Users/spider/REPOS/nightshift/src/nightshift/docs_gate.py --check --base bbe1b8faa2d34c3eefb57a97ee10e855f41bddfd`
- **#2** Add a host-check regression test for empty and unbalanced-quote check commands — `python -m pytest tests/test_host_check_malformed.py -q`
- **#3** Add a setup script smoke test for bash syntax and --help exit status — `python -m pytest tests/test_setup_help.py -q`

## Error

git add -- .nightshift/brief.json docs/adr/README.md docs/adr/0008-host-checks-fail-closed.md docs/adr/0009-writer-path-lock.md docs/adr/0010-scoped-commits-preserve-staged-work.md docs/adr/0011-cmm-evidence-histogram.md docs/adr/0012-portfolio-priorities-opt-in.md docs/adr/0013-interpreter-and-addopts-normalization.md docs/adr/0014-live-model-id-resolution.md docs/adr/0015-thinking-effort-token-budget.md docs/adr/0016-cooperative-halt.md docs/assumptions.md failed (1): The following paths are ignored by one of your .gitignore files:
.nightshift
hint: Use -f if you really want to add them.
hint: Disable this message with "git config set advice.addIgnoredFile false"

## Owner portfolio priority (scope for this night)
{
  "id": "nightshift",
  "group": "Focus",
  "allocation": 15,
  "night": "Start with one target and two checkable jobs per night. Produce branch, test log, summary and accepted/rejected outcome; run sequentially on shared hardware.",
  "human": "Review accepted outcomes and log minutes spent specifying, reviewing and repairing. Choose tasks by portfolio priority, not repository recency alone.",
  "gate": "Over two weeks, require positive net human time saved and no material scope violation. Count failed and abandoned runs. Pause expansion if review backlog exceeds one morning session.",
  "amend": "Designate as a cross-cutting Research Engine flagship for internal capacity, while keeping commercial ambition conditional. Limit self-improvement so the engine does not consume the lab.",
  "source": "https://raw.githubusercontent.com/sw30labs/.github/main/docs/porter-data.json",
  "sha256": "95865d11218bbec9f727a45e0c30b9faf42c28abebf2a318066b96fa01e6750c",
  "assessment_date": "2026-09-06",
  "selection_reason": "lowest attempted-nights / allocation; highest allocation breaks ties"
}
Choose only small host-checkable work advancing the night objective above. Do not substitute generic cleanup, add scope, change allocations, or execute the human/gate work. If prerequisite decisions are missing, halt with a concrete blocker. Porter pressure scores are market context, not execution rankings. TwinOps is background memory, not authority to override current owner priorities.
