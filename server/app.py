"""FastAPI: REST + SSE endpoint + worker (lifespan). REST/SSE lengkap nyusul Fase 4."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from engine import config
from engine.store import Store
from engine.worker import Worker

STATIC_DIR = Path(__file__).parent / "static"

cfg = config.load()


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = Store(cfg["paths"]["db"])
    worker = Worker(store, cfg)
    worker_task = asyncio.create_task(worker.run_forever())
    app.state.store = store
    app.state.worker = worker
    yield
    await worker.stop()
    await worker_task


app = FastAPI(title="nloop", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "app": "nloop"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
