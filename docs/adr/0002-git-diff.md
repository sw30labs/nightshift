# 0002. The product is a git diff

**Accepted** · 2026-09

## Context

An overnight agent that talks is a chatbot. The operator reviews in VS Code in the morning. GitHub is the remote. `main` is the human's.

## Decision

Work happens on `night/YYYY-MM-DD[-HHMM]`. Never commit to `main`/`master`. Never force-push, amend, or delete branches. Push is off unless `--push`.

Halt finishes the **current turn**, then writes summary + ledger. It does not kill mid-write. A live bag also sets `halt_bag`.

You merge, cherry-pick keepers, or `git branch -D`. Meta Nightshift nights are the same rule: orange on the RSI graph, you still merge.

## Consequences

Failed nights stay as branches until you drop them. Forum `merged` is evidence (default-branch ledger + brief provenance, or `forum mark-merged`) — never “HEAD is main”.
