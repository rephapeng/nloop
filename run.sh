#!/usr/bin/env bash
# nloop server + worker (worker jalan di lifespan FastAPI).
# host/port dari config.yaml (section server), bisa dioverride env HOST/PORT.
set -e
cd "$(dirname "$0")"
[ -d .venv ] && source .venv/bin/activate
HOST="${HOST:-$(python -c "from engine.config import load; print(load()['server']['host'])")}"
PORT="${PORT:-$(python -c "from engine.config import load; print(load()['server']['port'])")}"
exec uvicorn server.app:app --host "$HOST" --port "$PORT" "$@"
