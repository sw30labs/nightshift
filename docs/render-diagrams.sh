#!/bin/bash
# Render the stone/rust diagram HTML pages to PNG for GitHub.
set -euo pipefail
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

render() {
  local html="$1" png="$2" w="$3" h="$4"
  local tmp
  tmp="$(mktemp -t ns-diagram).png"
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars \
    --force-device-scale-factor=2 \
    --window-size="${w},${h}" \
    --default-background-color=e8dfd2 \
    --virtual-time-budget=8000 \
    --screenshot="$tmp" \
    "file://${ROOT}/${html}"
  mv "$tmp" "$png"
  echo "wrote $png ($(wc -c < "$png") bytes)"
}

render docs/roadmap-loop.html docs/roadmap-loop.png 1280 980
render docs/roadmap-cmm.html docs/roadmap-cmm.png 1280 960
render docs/readme-bag.html docs/readme-bag.png 1280 1020
