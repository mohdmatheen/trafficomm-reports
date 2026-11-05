#!/bin/sh
# One container runs the web app and the background worker so both share the same persistent disk
# (needed on Render/Railway, where a disk attaches to a single service).
set -e
python -m scripts.seed
python -m app.worker &
exec uvicorn app.web:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 2 --proxy-headers --forwarded-allow-ips='*'
