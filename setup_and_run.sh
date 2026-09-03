#!/usr/bin/env bash
# Set up Nightshift from a source checkout, run the test suite, and start the
# command deck.
#
# Usage:
#   ./setup_and_run.sh                 # conda env + tests + mock deck over ~/REPOS → :43171
#   ./setup_and_run.sh --live          # same, then the deck against oMLX + DS4
#   ./setup_and_run.sh --demo          # mock deck with the seeded widget repo only
#   ./setup_and_run.sh --setup-only    # conda env + deps + tests, no deck
#   ./setup_and_run.sh --no-tests      # skip pytest
#   ./setup_and_run.sh --no-browser    # accepted; the deck does not open a tab
#   ./setup_and_run.sh --port 43171
#   ./setup_and_run.sh up              # start deck only (no pip if env exists)
#   ./setup_and_run.sh stop            # stale nightshift serve only
#   ./setup_and_run.sh status          # env? port listening?
#   ./setup_and_run.sh --help
#
# Env overrides:
#   NIGHTSHIFT_CONDA_ENV    conda env name (default: nightshift)
#   NIGHTSHIFT_PYTHON       python version for `conda create` (default: 3.11)
#   NIGHTSHIFT_PORT         command deck port (default 43171; not LoopScope :7788)
#   NIGHTSHIFT_NO_BROWSER=1 same as --no-browser
#   NIGHTSHIFT_ROOTS        git roots to scan (default $HOME/REPOS)
#
# Default is mock against real git projects under NIGHTSHIFT_ROOTS. --demo
# is opt-in (seeded widget only). --live expects Mac oMLX and spark-serve ds4;
# it fills the two-brain URLs if unset.
# LoopScope is best-effort. If pip cannot reach GitHub, observe degrades.
set -euo pipefail

cd "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

CMD=run
LIVE=0
DEMO=0
SETUP_ONLY=0
RUN_TESTS=1
OPEN_BROWSER=1
SKIP_PIP=0
PORT="${NIGHTSHIFT_PORT:-43171}"
ENV_NAME="${NIGHTSHIFT_CONDA_ENV:-nightshift}"
PY_VERSION="${NIGHTSHIFT_PYTHON:-3.11}"

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
    --demo) DEMO=1 ;;
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

# ── conda ───────────────────────────────────────────────────────────────────
find_conda() {
  local candidate
  if [ -n "${CONDA_EXE:-}" ] && [ -x "${CONDA_EXE}" ]; then
    printf '%s\n' "$CONDA_EXE"
    return 0
  fi
  if command -v conda >/dev/null 2>&1; then
    command -v conda
    return 0
  fi
  for candidate in \
    "${HOME}/miniforge3/bin/conda" \
    "${HOME}/mambaforge/bin/conda" \
    "${HOME}/miniconda3/bin/conda" \
    "${HOME}/anaconda3/bin/conda"
  do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

conda_env_exists() {
  "$CONDA_BIN" env list | awk -v environment="$ENV_NAME" '
    $1 == environment { found = 1 }
    END { exit(found ? 0 : 1) }
  '
}

CONDA_BIN="$(find_conda)" || {
  echo "ERROR: conda was not found. Install Miniconda/Miniforge, or set CONDA_EXE." >&2
  exit 1
}

run_in_env() {
  "$CONDA_BIN" run --no-capture-output --name "$ENV_NAME" "$@"
}

# ── stale decks ─────────────────────────────────────────────────────────────
# A serve from an earlier run outlives the terminal that started it and keeps
# holding the port, which makes a fresh start look like a hang. Clear ours,
# refuse to fight anyone else's. Only processes whose executable is python or
# conda count, so a shell or editor whose command line merely mentions the
# file is never a kill target.
deck_pids() {
  local pid comm
  for pid in $(pgrep -f ' -m nightshift serve|[/ ]nightshift serve' 2>/dev/null || true); do
    [ "$pid" = "$$" ] && continue
    comm="$(ps -o comm= -p "$pid" 2>/dev/null || true)"
    case "${comm##*/}" in
      python | Python | python[0-9]* | conda | conda.exe) printf '%s\n' "$pid" ;;
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
  if conda_env_exists; then
    echo "conda env  $ENV_NAME ($("$CONDA_BIN" run --name "$ENV_NAME" python --version 2>&1))"
  else
    echo "conda env  missing ($ENV_NAME)"
  fi
  pids="$(deck_pids)"
  if [ -n "$pids" ]; then
    echo "serve      pids $(echo "$pids" | tr '\n' ' ')"
  else
    echo "serve      not running"
  fi
  leftover="$(port_listener)"
  if [ -n "$leftover" ]; then
    echo "port       $PORT listening"
    printf '%s\n' "$leftover"
  else
    echo "port       $PORT free"
  fi
  exit 0
fi

if [ "$CMD" = "up" ]; then
  RUN_TESTS=0
  if conda_env_exists; then
    SKIP_PIP=1
  fi
fi

# ── env ──────────────────────────────────────────────────────────────────
if conda_env_exists; then
  echo "==> Using existing conda env: $ENV_NAME"
else
  echo "==> Creating conda env: $ENV_NAME (python $PY_VERSION)"
  "$CONDA_BIN" create --yes --name "$ENV_NAME" --channel conda-forge \
    "python=$PY_VERSION" pip setuptools wheel
fi

if [ "$SKIP_PIP" -eq 0 ]; then
  echo "==> Installing nightshift (extras: dev)"
  run_in_env python -m pip install --quiet --upgrade pip
  run_in_env python -m pip install --quiet -e ".[dev]"
  echo "==> Installing loopscope (best-effort)"
  if ! run_in_env python -m pip install --quiet "git+https://github.com/sw30labs/loopscope.git"; then
    echo "==> WARN: loopscope pip install failed — observe degrades to JSONL" >&2
  fi
else
  echo "==> up: $ENV_NAME already present, skipping pip"
fi

export PYTHONPATH="${PWD}${PYTHONPATH:+:$PYTHONPATH}"

if [ "$RUN_TESTS" -eq 1 ]; then
  echo "==> Running test suite"
  run_in_env python -m pytest -q
fi

if [ "$SETUP_ONLY" -eq 1 ]; then
  echo "==> setup-only: command deck not started"
  exit 0
fi

export NIGHTSHIFT_PORT="$PORT"
export NIGHTSHIFT_API_KEY="${NIGHTSHIFT_API_KEY:-test}"
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
  echo "==> live command deck (oMLX + spark-serve ds4, roots ${NIGHTSHIFT_ROOTS:-$HOME/REPOS})"
  exec "$CONDA_BIN" run --no-capture-output --name "$ENV_NAME" \
    python -m nightshift serve --host 127.0.0.1 --port "$PORT"
fi

if [ "$DEMO" -eq 1 ]; then
  echo "==> mock demo command deck (seeded widget only)"
  exec "$CONDA_BIN" run --no-capture-output --name "$ENV_NAME" \
    python -m nightshift serve --mock --demo --host 127.0.0.1 --port "$PORT"
fi

echo "==> mock command deck (roots ${NIGHTSHIFT_ROOTS:-$HOME/REPOS})"
exec "$CONDA_BIN" run --no-capture-output --name "$ENV_NAME" \
  python -m nightshift serve --mock --host 127.0.0.1 --port "$PORT"
