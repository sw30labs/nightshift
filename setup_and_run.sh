#!/usr/bin/env bash
# Set up Nightshift from a source checkout, run the test suite, and start the
# command deck.
#
# Usage:
#   ./setup_and_run.sh                 # venv + tests + mock demo deck → :43171
#   ./setup_and_run.sh --live          # same, then the deck against oMLX + DS4
#   ./setup_and_run.sh --setup-only    # venv + deps + tests, no deck
#   ./setup_and_run.sh --no-tests      # skip pytest
#   ./setup_and_run.sh --no-browser    # accepted; the deck does not open a tab
#   ./setup_and_run.sh --port 43171
#   ./setup_and_run.sh up              # start deck only (no pip if .venv exists)
#   ./setup_and_run.sh stop            # stale nightshift serve only
#   ./setup_and_run.sh status          # venv? port listening?
#   ./setup_and_run.sh --help
#
# Env overrides:
#   NIGHTSHIFT_PYTHON       interpreter used to create the venv (>= 3.11)
#   NIGHTSHIFT_PORT         command deck port (default 43171; not LoopScope :7788)
#   NIGHTSHIFT_NO_BROWSER=1 same as --no-browser
#
# Default is mock + a seeded widget repo so the list is not empty. --live
# expects Mac oMLX and spark-serve ds4; it fills the two-brain URLs if unset.
# LoopScope is best-effort. If pip cannot reach GitHub, observe degrades.
set -euo pipefail

cd "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

CMD=run
LIVE=0
SETUP_ONLY=0
RUN_TESTS=1
OPEN_BROWSER=1
SKIP_PIP=0
PORT="${NIGHTSHIFT_PORT:-43171}"
VENV=.venv

usage() { awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"; }

require_value() {
  if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
    echo "ERROR: $1 needs a value" >&2
    exit 1
  fi
}

# Optional subcommands first so flags still parse: ./setup_and_run.sh up --live
case "${1:-}" in
  up | stop | status)
    CMD="$1"
    shift
    ;;
esac

while [ "$#" -gt 0 ]; do
  case "$1" in
    --live) LIVE=1 ;;
    --port)
      require_value "$@"
      PORT="$2"
      shift
      ;;
    --setup-only) SETUP_ONLY=1 ;;
    --no-tests) RUN_TESTS=0 ;;
    --no-browser) OPEN_BROWSER=0 ;;
    -h | --help)
      usage
      exit 0
      ;;
    up | stop | status)
      echo "ERROR: subcommand '$1' must come before flags (try --help)" >&2
      exit 1
      ;;
    *)
      echo "ERROR: unknown option '$1' (try --help)" >&2
      exit 1
      ;;
  esac
  shift
done

[ -n "${NIGHTSHIFT_NO_BROWSER:-}" ] && OPEN_BROWSER=0

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "ERROR: --port must be an integer between 1 and 65535" >&2
  exit 1
fi

if [ "$CMD" = "up" ]; then
  RUN_TESTS=0
  if [ -x "$VENV/bin/python" ]; then
    SKIP_PIP=1
  fi
fi

# ── stale decks ─────────────────────────────────────────────────────────────
# A serve from an earlier run outlives the terminal that started it and keeps
# holding the port, which makes a fresh start look like a hang. Clear ours,
# refuse to fight anyone else's. Only processes whose executable is python
# count, so a shell or editor whose command line merely mentions the file is
# never a kill target.
deck_pids() {
  local pid comm
  for pid in $(pgrep -f ' -m nightshift serve|[/ ]nightshift serve' 2>/dev/null || true); do
    [ "$pid" = "$$" ] && continue
    comm="$(ps -o comm= -p "$pid" 2>/dev/null || true)"
    case "${comm##*/}" in
      python | Python | python[0-9]*) printf '%s\n' "$pid" ;;
    esac
  done
}

stop_stale_decks() {
  local pids
  pids="$(deck_pids)"
  if [ -n "$pids" ]; then
    echo "==> Stopping stale nightshift serve process(es): $(echo "$pids" | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 2
    pids="$(deck_pids)"
    if [ -n "$pids" ]; then
      # shellcheck disable=SC2086
      kill -9 $pids 2>/dev/null || true
      sleep 1
    fi
  fi
}

assert_port_ours_or_free() {
  if command -v lsof >/dev/null 2>&1 &&
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: port $PORT is held by a process that is not a nightshift serve:" >&2
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
    echo "Stop it, or set NIGHTSHIFT_PORT / --port to a free port." >&2
    exit 1
  fi
}

port_listener() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true
  fi
}

if [ "$CMD" = "stop" ]; then
  stop_stale_decks
  leftover="$(port_listener)"
  if [ -n "$leftover" ]; then
    echo "==> port $PORT still listening (not a nightshift serve we would kill):"
    printf '%s\n' "$leftover"
  else
    echo "==> no nightshift serve; port $PORT is free"
  fi
  exit 0
fi

if [ "$CMD" = "status" ]; then
  if [ -x "$VENV/bin/python" ]; then
    echo "venv     $VENV ($("$VENV/bin/python" --version 2>&1))"
  else
    echo "venv     missing ($VENV)"
  fi
  pids="$(deck_pids)"
  if [ -n "$pids" ]; then
    echo "serve    pids $(echo "$pids" | tr '\n' ' ')"
  else
    echo "serve    not running"
  fi
  leftover="$(port_listener)"
  if [ -n "$leftover" ]; then
    echo "port     $PORT listening"
    printf '%s\n' "$leftover"
  else
    echo "port     $PORT free"
  fi
  exit 0
fi

# ── interpreter ─────────────────────────────────────────────────────────────
# pyproject requires >= 3.11. Prefer the newest on PATH rather than pinning;
# there is no upper bound. macOS /usr/bin/python3 is often still 3.9 and
# cannot import the package.
pick_python() {
  local candidate
  if [ -n "${NIGHTSHIFT_PYTHON:-}" ]; then
    if [ ! -x "${NIGHTSHIFT_PYTHON}" ] && ! command -v "${NIGHTSHIFT_PYTHON}" >/dev/null 2>&1; then
      echo "ERROR: NIGHTSHIFT_PYTHON is not executable: $NIGHTSHIFT_PYTHON" >&2
      exit 1
    fi
    if ! "$NIGHTSHIFT_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      echo "ERROR: NIGHTSHIFT_PYTHON must be Python 3.11 or newer" >&2
      exit 1
    fi
    printf '%s\n' "$NIGHTSHIFT_PYTHON"
    return
  fi
  if [ -x "$VENV/bin/python" ] &&
    "$VENV/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    printf '%s\n' "$VENV/bin/python"
    return
  fi
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      printf '%s\n' "$candidate"
      return
    fi
  done
  echo ""
}

PY="$(pick_python)"
if [ -z "$PY" ]; then
  echo "ERROR: no Python >= 3.11 on PATH. Set NIGHTSHIFT_PYTHON." >&2
  exit 1
fi
echo "==> Using $PY ($("$PY" --version 2>&1))"

if [ -x "$VENV/bin/python" ] &&
  ! "$VENV/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  echo "==> Existing $VENV is Python < 3.11 — recreating"
  rm -rf "$VENV"
fi

if [ ! -d "$VENV" ]; then
  echo "==> Creating virtual environment in $VENV"
  "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

if [ "$SKIP_PIP" -eq 0 ]; then
  echo "==> Installing nightshift (extras: dev)"
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -e ".[dev]"
  echo "==> Installing loopscope (best-effort)"
  if ! python -m pip install --quiet "git+https://github.com/sw30labs/loopscope.git"; then
    echo "==> WARN: loopscope pip install failed — observe degrades to JSONL" >&2
  fi
else
  echo "==> up: $VENV already present, skipping pip"
fi

export PYTHONPATH="${PWD}${PYTHONPATH:+:$PYTHONPATH}"

if [ "$RUN_TESTS" -eq 1 ]; then
  echo "==> Running test suite"
  python -m pytest -q
fi

if [ "$SETUP_ONLY" -eq 1 ]; then
  echo "==> setup-only: command deck not started"
  exit 0
fi

export NIGHTSHIFT_PORT="$PORT"
if [ "$OPEN_BROWSER" -eq 0 ]; then
  export NIGHTSHIFT_NO_BROWSER=1
fi

if [ "$LIVE" -eq 1 ]; then
  export NIGHTSHIFT_WRITER_BASE_URL="${NIGHTSHIFT_WRITER_BASE_URL:-http://192.168.86.44:8000/v1}"
  export NIGHTSHIFT_WRITER_MODEL="${NIGHTSHIFT_WRITER_MODEL:-deepseek-v4-flash}"
  export NIGHTSHIFT_CRITIC_BASE_URL="${NIGHTSHIFT_CRITIC_BASE_URL:-http://127.0.0.1:8000/v1}"
  export NIGHTSHIFT_CRITIC_MODEL="${NIGHTSHIFT_CRITIC_MODEL:-GLM-5.3-Flash-MLX-8bit}"
  echo "==> live brains"
  echo "    writer  $NIGHTSHIFT_WRITER_MODEL @ $NIGHTSHIFT_WRITER_BASE_URL"
  echo "    critic  $NIGHTSHIFT_CRITIC_MODEL @ $NIGHTSHIFT_CRITIC_BASE_URL"
fi

stop_stale_decks
assert_port_ours_or_free

echo "==> Command deck at http://127.0.0.1:$PORT (Ctrl+C to stop)"
echo "    headless? tunnel with:  ssh -L $PORT:127.0.0.1:$PORT <host>"
# --no-browser is accepted for script parity; nightshift serve does not open a tab.

if [ "$LIVE" -eq 1 ]; then
  echo "==> live command deck (oMLX + spark-serve ds4)"
  exec python -m nightshift serve --host 127.0.0.1 --port "$PORT"
fi

echo "==> mock demo command deck"
exec python -m nightshift serve --mock --demo --host 127.0.0.1 --port "$PORT"
