# Nightshift — 2026-10-27

**2 of 2 landed · 0 void · 0 open — brief exhausted**

**Repo:** `/Users/spider/REPOS/nightshift`
**Branch:** `night/2026-10-27`
**Halt:** remaining_zero
**Remaining:** 0
**Base:** main @ 1774ce9
**Main:** main @ 1774ce9 (unchanged)
**Host python:** /Users/spider/miniconda3/envs/nightshift/bin/python (conda env: dir name)

## Frozen brief

- **#1 [done]** Add malformed host-check regression tests
  - check: `python -m pytest tests/test_host_check_malformed.py -q`
  - paths: tests/test_host_check_malformed.py, src/nightshift/host.py
  - note: landed turn 1
  - turns: 1
  - commits: bd99f6b
- **#2 [done]** Add setup_and_run.sh smoke test
  - check: `python -m pytest tests/test_setup_help.py -q`
  - paths: tests/test_setup_help.py, setup_and_run.sh
  - note: landed turn 2
  - turns: 1
  - commits: 8c1c19d

## Voided / skipped-as-duplicate

- none

## What changed

```
8c1c19d nightshift: turn 2 — Add tests/test_setup_help.py smoke test that runs setup_and_run.sh --hel
bd99f6b nightshift: turn 1 — Add malformed host-check regression tests in tests/test_host_check_malfo
98a08ed nightshift: freeze brief (2 upgrades)
```

```
tests/test_host_check_malformed.py | 58 ++++++++++++++++++++++++++++++++++++++
 tests/test_setup_help.py           | 25 ++++++++++++++++
 2 files changed, 83 insertions(+)
```

`git diff 1774ce9...night/2026-10-27 -- . ':!.nightshift'`

## Land it

- merge: `git checkout main && git merge --no-ff night/2026-10-27`
- cherry-pick keepers: `git cherry-pick bd99f6b 8c1c19d`
- drop: `git branch -D night/2026-10-27`
- `.nightshift/ledger.json` rides along on merge

## What the critic refused

- turn 1
  - Upgrade 1 host check passed: 12 tests passed.
  - Only tests/test_host_check_malformed.py was written, which is within current_job_paths.
  - No out-of-scope project file writes detected.
- turn 2
  - Host check for upgrade 2 passed: python -m pytest tests/test_setup_help.py -q exited 0 with 1 passed.
  - Current turn added tests/test_setup_help.py, which is within current_job_paths.
  - setup_and_run.sh was not modified, which is acceptable because the smoke test passed.
  - .nightshift/brief.json and .nightshift/turns.jsonl changes are orchestration state from prior accepted work, not current writer project-body edits, so they are not reverted.

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
