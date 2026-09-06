# Portfolio priorities → Nightshift → Morning Prayers

## Purpose and authority

The owner sets direction. Nightshift turns current portfolio priorities into a bounded nightly bag and produces evidence and reviewable branches. Morning Prayers is the GrokBot surface for human-on-the-loop (HOTL) decisions. TwinOps supplies personal background; Singularity Atlas supplies sourced external signals. Neither silently rewrites owner priorities.

Keep three separate concepts:

- **Portfolio / research landscape:** what SW30 Labs contains and explores. Preserve the existing GitHub Pages landscape.
- **Flagships:** projects that represent the lab; designation does not imply maturity or an automatic nightly slot.
- **Portfolio priorities:** the current allocation, rationale, bounded work and decision gates. These determine execution.

The existing [portfolio assessment page](https://sw30labs.github.io/.github/porter.html) is backed by `sw30labs/.github/docs/porter-data.json`. Nightshift reads that JSON directly, never scrapes rendered HTML or edits the landscape. The Porter five-force scores describe competitive pressure; they are not a highest-score-first task ranking. Use the owner's amended decision, allocation, `night`, `human`, and `gate` fields.

## Implemented selection contract

Opt in with `~/.nightshift/portfolio.json`:

```json
{
  "source": "https://raw.githubusercontent.com/sw30labs/.github/main/docs/porter-data.json",
  "paused": true,
  "max_pending_reviews": 1,
  "repo_map": {"rpc-h16": "sw30labs/rpc-h16"},
  "approved_assessments": {}
}
```

Every `nightshift bag` selection (CLI or deck) fetches the current source with a timeout and validates it. A failure stops selection; it never falls back to recency or cached authority. `priority-error.json` explains the failure. Valid snapshots are archived under `priority-snapshots/<sha256>.json`; bag records retain the digest, fetch time, source and assessment date. An old assessment date is visible provenance, not automatically expiry: do not invent a new strategy just because the owner has not revised it. Successful fetching is distinct from substantive review freshness.

Only positive allocations outside Gate/Park are eligible. Frontier work additionally needs `approved_assessments[project_id]` equal to the current source SHA-256 after the owner approves its assumptions. Any source change invalidates that approval conservatively. Zero-allocation Rotate projects wait for a revised owner allocation. Unknown groups or malformed data stop selection.

Repositories match their GitHub origin identity, not their local directory name. A missing, dirty, detached, in-progress or ambiguous checkout is reported and skipped; nothing is cloned, unarchived or reset automatically. The RPC mapping stays in local configuration because the public dossier intentionally links public research context instead of private source.

The initial portfolio contract is **one target, two checkable jobs, sequential execution**. Nightshift has no automatic self-improvement slot; it competes under its own allocation. Among eligible projects, prefer the smallest historical attempted-night count divided by current allocation, then larger allocation, then stable project ID. This approximates relative service across nights; it is not an exact percentage of hours or human attention. Do not interpret unavailable allocations as evidence of completed work. No machinery assigns the unallocated portion of the portfolio to arbitrary tasks.

Previewing a bag does not consume service credit. Beginning a target records an attempt even if it fails, so failures do not vanish from planning. Selection stops when the configured pending-review limit is reached. The owner can accept, reject or defer through Morning Prayers; deferral deliberately leaves the review pending. Counts are long-lived attempted nights, not a moving weekly hour budget; re-evaluate this simple policy using actual evidence before adding scheduling machinery.

The selected `night` objective and its `human`/`gate` boundaries enter the critic's freeze context and the night summary. Host commands, path locks, turn limits, deadline and no-main-write rules still apply. The LLM's interpretation of strategic alignment is not a formal guarantee: review the frozen brief and delivered changes during HOTL. This first integration does not ingest GitHub issues or change their status automatically.

The supplied configuration preserves the org-refactor pause. `bag` can preview current priorities; `bag --run` refuses while `paused` is true. Change it to false only after the owner explicitly resumes the nightly routine. This does not itself install or resume a scheduler.

## Morning Prayers handoff

Read `~/.nightshift/morning-prayers.md` after a bag and run:

```bash
nightshift morning --portfolio
```

The report links the priority version, objective, gate, selected/skipped targets, branch and job outcomes. Inspect each target's `.nightshift/summary.md`, host logs and diff. A host-passing job is a candidate keeper, not an accepted or merged outcome; the existing internal `landed` count means verified jobs.

Record the owner's decision after review:

```bash
python -m nightshift.priorities review BAG_ID PROJECT_ID accept --minutes 12 --note "Reviewed diff and checks; accepted candidate, merge handled separately"
python -m nightshift.priorities review BAG_ID PROJECT_ID reject --minutes 8 --note "Check passed but did not advance the chosen objective"
python -m nightshift.priorities review BAG_ID PROJECT_ID defer --minutes 5 --note "Need to inspect the failed case"
```

These commands record a disposition only. They do not merge, push, delete branches, approve hardware or change allocations. Actual minutes include review/repair as described in the note. Computing net human time saved still needs a credible manual-work comparison; do not infer savings from generated commits or GPU utilization.

### GrokBot instruction amendment, ready to apply after the current pause

> At Morning Prayers, first read the latest Nightshift priority report and the existing forum/summary. State which priority revision was used, what strategic objective the work served, what passed or failed, and what remains blocked. Present small accept/reject/defer decisions with diffs and evidence. Record Nic's disposition and review minutes using the review command. Keep merging/publishing subject to the current owner authorization; a successful check is not permission to land. Feed repeated failures, review burden and changed assumptions into the weekly portfolio review. Do not silently promote a flagship, increase Nightshift's allocation, or turn Atlas news into work orders. Preserve any explicit pause until Nic clears it.

> For the midnight routine after resume: run the installed `nightshift bag --run` against the configured Nightshift home. It refreshes owner priorities before selection and freezes that snapshot for the bag. Do not reuse yesterday's target list or bypass pending HOTL decisions. If the source, hardware, host connection or eligible checkout is unavailable, report the blocker rather than substituting unrelated maintenance.

The current GrokBot Morning Prayers conversation was inspected on 2026-09-06. Its latest recorded instruction says midnight Nightshift and publishing are paused during the organization refactor. This change does not resume that routine or send messages to bots. The handoff above is prepared, not silently installed into GrokBot's agent instructions.

## Cadence: operations daily, allocations weekly, Porter monthly

| Cadence | Inputs | Output and owner decision |
|---|---|---|
| Every night | Fresh priority source, eligible clean checkouts, pending HOTL dispositions, prior attempted service | One bounded bag with a frozen source digest; branches and evidence, or an explicit no-run reason. |
| Every morning, about 10–15 minutes | Actual diff/checks, failures, strategic fit and review effort | Accept/reject/defer; resolve blockers; keep only useful work. Routine triage does not rewrite strategy. |
| Weekly, about 30–45 minutes | Planned versus achieved outcomes, all attempted nights, accepted/rejected/deferred work, review backlog, human effort and Atlas themes | Owner-approved allocations and next bounded objectives; note what changed, why, and which evidence/gate drove it. |
| Monthly, about 60 minutes | Existing Porter assessments plus changed competitor, substitute, supplier, buyer or entry-barrier evidence | Refresh each affected force, confidence and strategic implication; refresh the full portfolio overview. |
| Event-driven | Material licensing/dependency change, major competing capability, concrete user feedback, reproduced failure or decisive experiment | Targeted reassessment immediately; owner decides whether priorities change before the next night. |

Suggested weekly scorecard: project; intended outcome; actual evidence; attempted nights; accepted/rejected/deferred outcomes; review/repair minutes; remaining assumption; relevant Atlas signal; proposed keep/increase/reduce/pause decision. Record factual outcomes separately from proposed allocation changes. The current service ledger provides attempts and recorded review decisions/minutes; the rest is deliberate synthesis, not claimed automatic analytics.

Atlas should contribute a short shortlist (for example three themes), each with source/date, what changed, affected project and Porter force, confidence, and a concrete implication or proposed test. Repeated headlines are not independent evidence. Discard themes with no portfolio consequence. A single vendor announcement is a lead to verify, not proof of an advantage. This integration does not add a news fetcher or automatically run Atlas; it defines the handoff to the existing Atlas/Morning Prayers workflow.

TwinOps grounding uses read-only queries and records provenance. GrokBot's documented TwinOps integration uses a parquet snapshot, whereas the local TwinOps stdio MCP queries Neo4j; check freshness and do not call them interchangeable live sources. During implementation, the local MCP returned relevant digital-twin/agent article metadata and no `Nightshift` document. No private messages or personal graph rows are copied into public code or reports. Current owner decisions override historical memory.

## Limits and first validation

This is a bounded integration of priority refresh, selection, scope context and HOTL reporting into the existing engine. It is not a new autonomous portfolio strategist, GitHub task synchronizer or automatic merge service. Keep the existing scheduler paused during the refactor. Once explicitly resumed, start with one bag, review the evidence the next morning, and evaluate net useful human time over two weekly cycles. Expand only if review cost and accepted outcomes justify it.
