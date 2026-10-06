#!/usr/bin/env bash
# One-command start: serve API + SPA (builds the dashboard first if the
# committed bundle is missing).
#
#   scripts/run.sh                # http://127.0.0.1:8000
#   PORT=9000 scripts/run.sh      # different port (HOST=0.0.0.0 to expose)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f frontend/dist/index.html ]; then
  if [ ! -d frontend/node_modules ]; then
    echo "[run] installing frontend dependencies"
    (cd frontend && npm ci)
  fi
  echo "[run] building frontend"
  (cd frontend && npm run build)
fi

if [ -d backend/.venv ]; then
  # shellcheck disable=SC1091
  source backend/.venv/bin/activate
fi

cd backend
exec python3 -m rat "$@"
