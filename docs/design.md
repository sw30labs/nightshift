# Design

Overnight **areas of improvement** across a portfolio. Not a chatbot. The product is a `git diff`.

You pick a clone, or tonight's **bag**. A critic freezes 2–5 checkable upgrades. A writer works them on the cheap-electricity tariff. Morning you have `night/YYYY-MM-DD` branches. **`main` is never touched.** You merge, cherry-pick, or delete.

## Not

- Not two agents on one GPU.
- Not DE/OE checkboxes. JOBS N is the whole bag.
- Not a cloud app. Deck is stdlib HTTP on `127.0.0.1:43171`.
- Not invented maturity scores. CMM is empty until the forum has rows.
- Not a hard kill. Halt finishes the current turn.

## Contract

1. **Minute 0 is critic-only.** Walk the tree, tests, README, log, clone+home ledger, other-repo forum excerpt. Freeze against current HEAD. Then branch. Freeze failure = still on base, no orphan branch.
2. **The brief cannot grow.** Writer cannot add upgrades. Critic cannot expand scope at 3am. Void can shrink.
3. **Host pytest is truth.** Only the current job can be marked done, and only if its `paths[]` changed this night. Same host failure three times voids that job.
4. **Writer edits. Critic never writes the project body.** Unapproved paths are reverted; the restored tree is rechecked before scoring.
5. **Publish after halt**, never from the writer, never at freeze, never on `--dry-run`.

## Two lenses, one freeze

**DE** — is the control *as written* checkable tonight.
**OE** — memory of it running (ledger). No evidence in the clone means no OE item.

## Two brains

| | Machine | Default | Tools |
|---|---|---|---|
| Writer | Spark DS4 | `:8000` `auto` | files inside the target. No network. |
| Critic | Mac oMLX | `:8000` GLM-5.3 | inspect, score, slash, revert, halt. |

Do not point both at the same server.

## Morning

Per target: branch, LoopScope replay, `.nightshift/summary.md`.
Estate: `forum.md`, CMM histogram, land commands (`nightshift morning --portfolio`).

You still decide what lands.

Map: [architecture.md](architecture.md) · decisions: [adr/](adr/) · later: [../ROADMAP.md](../ROADMAP.md)
