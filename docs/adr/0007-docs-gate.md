# 0007. ADRs and assumptions first

**Accepted** · 2026-10

## Context

Most clones under `~/REPOS` have no ADR folder, and none has an assumptions register. Without them a night cannot tell whether a change fits the project, and the morning review has nothing to align against. The owner's rule: ADRs are non-negotiable.

## Decision

At freeze, before the critic proposes anything, `docs_gate.audit` checks the clone deterministically (no model):

- an ADR folder under `docs/` (`adr`, `adrs` or `decisions`) with at least one ADR, and
- an assumptions register under `docs/` (a markdown file whose name contains `assumption`) with at least one row or item.

If either is missing, Nightshift freezes **upgrade 1** itself: retrospective ADRs plus the register. Paths are the existing ADR folder (else `docs/adr/`) and the existing register (else `docs/assumptions.md`). The writer reads the whole snapshot, writes one ADR per real decision seen in code, config, commits or docs, cites evidence, labels unstated rationale `(inferred)`, and never invents a decision. New ADRs follow the folder's own format; with no folder, the Porter Forces format (`NNN-title.md`, status, date, owners, Context, Decision, Alternatives considered, Consequences, Evidence).

The check is `python …/docs_gate.py --check --base <night parent>`. ADR files added since the parent must have a status line, Context, Decision and Evidence sections, and a link from the folder's `README.md` index. Existing hand-written ADRs in another house style are never a gap; they only show up as notes.

The critic still proposes JOBS N, so the gate is **additional** (N+1), except at JOBS 5 where the critic proposes 4. This amends 0003: JOBS N is the critic's share. The gate is exempt from `failed_before` history voids (it re-audits every night until met); `dirty_in_tree` and three identical host failures still void it. Same branch, critic, host truth and no-push rules as any job. `NIGHTSHIFT_DOCS_GATE=0` turns it off.

## Consequences

Every undocumented clone spends its first job on docs until the gate is met. Large repos are bounded by the writer snapshot (`NIGHTSHIFT_WRITER_SNAPSHOT_CHARS`), so "read the whole project" means the whole snapshot; a big clone may need several nights. The check proves shape and evidence headings, not that every decision was captured; that is the critic's and the morning review's job.
