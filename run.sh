#!/usr/bin/env bash
# nloop server + worker (worker jalan di lifespan FastAPI).
set -e
cd "$(dirname "$0")"
[ -d .venv ] && source .venv/bin/activate
exec uvicorn server.app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8484}" "$@"
