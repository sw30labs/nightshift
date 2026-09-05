# 0006. Local command deck

**Accepted** · 2026-09

## Context

The operator is on the Mac that already runs oMLX. Morning review is VS Code. A React/Next deck would be a second product.

## Decision

Stdlib `ThreadingHTTPServer`. No React, no Vue, no Next, no Tailwind-as-a-framework. Bind `127.0.0.1:43171`. Stone/rust HTML in `deck.html`. CLI verbs are equally first-class.

POST `/api/run`, `/api/bag`, `/api/halt` start local code. Require `Content-Type: application/json`, reject cross-origin `Origin`, cap body size. Dry-run previews stay on screen while status polls. Halt requested stays on the board after `halt.request` is consumed.

`--mock` / `--demo` for machines without GPUs. Live confirm a night started is GPU activity, not a Run click.

## Consequences

No auth cookies, no cloud. Same-origin fetch from the served page is enough. Aineko far right on the GUI, left of the title on GitHub READMEs.
