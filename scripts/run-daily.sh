#!/usr/bin/env bash
# Cron / systemd: uma vez ao dia. Processa 1 vídeo por vez até acabar os pendentes do dia.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"
if [[ -x .venv/bin/python ]]; then
  PY="$PWD/.venv/bin/python"
else
  PY=python
fi
exec "$PY" -m backend.app.cli run --max 1
