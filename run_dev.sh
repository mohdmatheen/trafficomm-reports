#!/usr/bin/env bash
# Start the web app and the background worker locally (Ctrl+C stops both).
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && { set -a; . ./.env; set +a; }
export PYTHONPATH=.
python -m scripts.seed
python -m app.worker & WPID=$!
trap 'kill $WPID 2>/dev/null' EXIT
python -m uvicorn app.web:app --host 127.0.0.1 --port "${PORT:-8000}"
