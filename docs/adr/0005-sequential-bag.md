# 0005. Sequential bag

**Accepted** · 2026-09

## Context

The overnight tariff is ~8 h. One target leaves the rest of `~/REPOS` dark. Two concurrent Ralphs cannot share one writer and one critic (0001).

## Decision

`nightshift bag` picks 2 nights (max 3): recency, CMM holes, optional `prior.json` liked/skip. **Always include meta Nightshift** unless `--skip-meta`. Sequential `run_night` against the one writer and one critic, shared 06:00 deadline.

Durable lock: `bag.json` `state=running` + live pid, acquired under `bag.lock`. CLI `run`, deck RUN, RUN BAG all refuse while it is held — including the gap between nights. `run_bag` → `run_night` passes `allow_self_bag=True`. Stale recovery uses `pid_alive`; this process is never dead.

One target crash does not abort the bag. Halt/clock skip the rest. Ctrl-C ⇒ bag `halted`, not `done`.

Rejected for v0: parallel Ralphs, queue fields on `RunStatus`.

## Consequences

LoopScope is one dashboard for the bag; inner nights are nested runs, not a second `:7788`. Deck Aineko WATCH while a night **or** a bag is running.
