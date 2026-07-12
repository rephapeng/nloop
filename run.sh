#!/usr/bin/env bash
# nloop server (Fase 0). Worker nyusul di Fase 3.
set -e
cd "$(dirname "$0")"
[ -d .venv ] && source .venv/bin/activate
exec uvicorn server.app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8484}" "$@"
