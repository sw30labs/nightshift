# 0004. Forum is the estate

**Accepted** · 2026-09

## Context

Per-clone `.nightshift/ledger.json` (plus `~/.nightshift/ledger/<sha12>.json`) is the void prior for **one tree**. Mixing portfolio grain into it breaks history-void and deleted-night tests. Putting the forum inside a clone is another silo.

## Decision

`~/.nightshift/forum.json` + `forum.md` is the shared ledger. Publish after halt from `NightReport` + the ledger rows just written. Never from the writer. Never at freeze. Never dry-run.

Freeze snapshot gets a ranked 8 KB **other-repo** excerpt. Writer snapshots get `home=` (OE) but not `forum=`.

L4 reuse is exact `check_hash+paths` across `repo_id`s, computed only inside `publish_night`. Do not void from the forum in v0. Do not clobber a `done` item with a later same-key void.

**CMM** is a pure function over the forum. Empty columns until evidence. No LLM scores. Atlas ingest is Later; Nightshift does not import Atlas.

Rejected for v0: SQLite, merging into home ledger shards, embeddings.

## Consequences

Ingest is a latest-entry projection, read-only on clones. Gone clones are orphans, not a crash. Schema v1 JSON, flock + atomic replace.
