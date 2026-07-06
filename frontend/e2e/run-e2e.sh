#!/usr/bin/env bash
# Start a fresh backend + frontend pair for Playwright, run the tests,
# tear everything down.
#
# DATA_DIR is forced to /tmp/accounting-e2e-data — a throwaway tree
# outside the project so no test artefacts leak into your real
# data directory. The dir is wiped at the start of every run.
#
# Logs land in /tmp/accounting-e2e-logs/ for post-mortem.
#
# Usage:
#   cd frontend && ./e2e/run-e2e.sh                             # all tests
#   cd frontend && ./e2e/run-e2e.sh 00-fresh-bootstrap.spec.ts          # single file
#   cd frontend && ./e2e/run-e2e.sh -g "create a company"       # name filter
#   cd frontend && KEEP_RUNNING=1 ./e2e/run-e2e.sh              # leave servers up after

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
DATA_DIR="/tmp/accounting-e2e-data"
LOG_DIR="/tmp/accounting-e2e-logs"
BACKEND_PORT=8765
FRONTEND_PORT=5174

mkdir -p "$LOG_DIR"
rm -rf "$DATA_DIR"
mkdir -p "$DATA_DIR"
# Wipe stale logs from the previous run so a failure inspection always
# sees the current run's output.
rm -f "$LOG_DIR"/*.log

# Pre-init PIDs so the cleanup trap can run safely even if the script
# exits before the backend / frontend start (set -u would trip on
# unbound variables otherwise).
BACKEND_PID=""
FRONTEND_PID=""

cleanup() {
  if [[ "${KEEP_RUNNING:-0}" == "1" ]]; then
    echo "KEEP_RUNNING=1 — leaving servers up. PIDs:"
    [[ -n "$BACKEND_PID" ]] && echo "  backend: $BACKEND_PID"
    [[ -n "$FRONTEND_PID" ]] && echo "  frontend: $FRONTEND_PID"
    return
  fi
  echo ">> Stopping servers"
  [[ -n "$BACKEND_PID" ]] && kill "$BACKEND_PID" 2>/dev/null || true
  [[ -n "$FRONTEND_PID" ]] && kill "$FRONTEND_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT

echo ">> Starting backend on :$BACKEND_PORT (DATA_DIR=$DATA_DIR)"
cd "$REPO/backend"
DATA_DIR="$DATA_DIR" \
  python3 -m uvicorn app.main:app \
    --host 127.0.0.1 --port $BACKEND_PORT --no-access-log \
    > "$LOG_DIR/backend.log" 2>&1 &
BACKEND_PID=$!

echo ">> Waiting for backend /health"
for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null; then
    echo "   backend up"
    break
  fi
  sleep 0.5
done

if ! curl -sf "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null; then
  echo "ERROR: backend never came up. Logs:"
  tail -50 "$LOG_DIR/backend.log"
  exit 1
fi

echo ">> Starting Vite on :$FRONTEND_PORT (proxying to :$BACKEND_PORT)"
cd "$ROOT"
# Override the dev server port + proxy target via env so we don't
# clash with a developer's own running stack.
VITE_E2E_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT" \
  npx vite --host 127.0.0.1 --port $FRONTEND_PORT --strictPort \
    > "$LOG_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!

echo ">> Waiting for Vite (cold deps optimisation can take a while)"
for i in $(seq 1 120); do
  if curl -sf "http://127.0.0.1:$FRONTEND_PORT/" >/dev/null; then
    echo "   vite up"
    break
  fi
  # After 15 s, print a hint so the wait doesn't look like a hang.
  if [[ $i -eq 30 ]]; then
    echo "   still waiting for Vite (likely first-run dep optimisation)…"
  fi
  sleep 0.5
done

if ! curl -sf "http://127.0.0.1:$FRONTEND_PORT/" >/dev/null; then
  echo "ERROR: vite never came up after 60s. Logs:"
  tail -50 "$LOG_DIR/frontend.log"
  exit 1
fi

echo ">> Running Playwright"
cd "$ROOT"
E2E_BASE_URL="http://127.0.0.1:$FRONTEND_PORT" \
  npx playwright test "$@"
