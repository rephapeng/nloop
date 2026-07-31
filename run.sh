#!/usr/bin/env bash
# nloop server + worker (the worker runs inside the FastAPI lifespan).
# host/port come from config.yaml (the server section), overridable with the HOST/PORT env vars.
set -e
cd "$(dirname "$0")"
[ -d .venv ] && source .venv/bin/activate
HOST="${HOST:-$(python -c "from engine.config import load; print(load()['server']['host'])")}"
PORT="${PORT:-$(python -c "from engine.config import load; print(load()['server']['port'])")}"
exec uvicorn server.app:app --host "$HOST" --port "$PORT" "$@"
