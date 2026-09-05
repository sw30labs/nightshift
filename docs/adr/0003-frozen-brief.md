# 0003. Frozen brief, host is truth

**Accepted** · 2026-09

## Context

A writer that can invent extra work at 3am gold-plates until the clock. A critic that marks done from its own opinion false-greens.

## Decision

Minute 0 freezes **2–5** upgrades (`JOBS`, default 2). After freeze the list cannot grow. Void can shrink (`duplicate_of_history`, `failed_before`, `dirty_in_tree`, three identical host failures).

Host pytest is truth. Only the **current** job can be marked `done`, and only if its `paths[]` changed this night vs the night parent. Critic `passed_ids` cannot override a red check. `"halt": "false"` is not halt.

After reverting unapproved files, recheck the restored tree before scoring.

## Consequences

JOBS N is the total bag — not DE + OE separately, no checkboxes. Writer timeouts and non-JSON replies retry next turn; they do not kill the night.
