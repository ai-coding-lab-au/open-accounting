#!/usr/bin/env bash
# Dev launcher: backend on :8787 + frontend on :5173.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-$PWD/backend/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || command -v python)"
fi

(cd backend && exec "$PYTHON" -m uvicorn app.main:app --port 8787 --reload) &
BACK=$!
trap 'kill $BACK 2>/dev/null' EXIT

cd frontend
npm run dev
