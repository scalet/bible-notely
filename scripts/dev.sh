#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"
if [[ -x .venv/bin/python ]]; then
  PY="$PWD/.venv/bin/python"
else
  PY=python3
fi
"$PY" -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000 &
API_PID=$!
trap 'kill $API_PID' EXIT
cd frontend
echo "Interface: http://localhost:5173"
npm run dev
