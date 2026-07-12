"""FastAPI: REST + SSE endpoint. Fase 0: health + static. REST/SSE nyusul Fase 4."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from engine import config

STATIC_DIR = Path(__file__).parent / "static"

cfg = config.load()
app = FastAPI(title="nloop")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "app": "nloop"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
