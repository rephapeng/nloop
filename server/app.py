"""FastAPI: REST + SSE + worker (lifespan).

SSE flow: replay event tersimpan dari DB (cursor `?after=<id>`) → subscribe bus →
stream live. Event live dengan id <= cursor replay di-skip (dedupe race).
Run yang udah final: replay lalu tutup.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine import config
from engine.events import EventBus
from engine.store import Store
from engine.worker import Worker

STATIC_DIR = Path(__file__).parent / "static"
TERMINAL = ("succeeded", "failed", "stopped")
KEEPALIVE_SEC = 15


class LoopCreate(BaseModel):
    goal: str = Field(min_length=1)
    verify_cmd: str = Field(min_length=1)
    workdir: str | None = None      # default: workspaces/<id> dibikinin
    model: str | None = None
    max_iterations: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)


def create_app(cfg: dict | None = None) -> FastAPI:
    cfg = cfg or config.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(cfg["paths"]["db"])
        bus = EventBus()
        worker = Worker(store, cfg, on_event=bus.publish)
        worker_task = asyncio.create_task(worker.run_forever())
        app.state.store, app.state.bus, app.state.worker = store, bus, worker
        yield
        await worker.stop()
        await worker_task

    app = FastAPI(title="nloop", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "app": "nloop"}

    @app.post("/api/loops", status_code=201)
    def create_loop(body: LoopCreate, request: Request) -> dict:
        store: Store = request.app.state.store
        loops_cfg = cfg["loops"]

        workdir = body.workdir
        if workdir is None:
            workdir = os.path.join(cfg["paths"]["workspaces"], uuid.uuid4().hex[:8])
            os.makedirs(workdir, exist_ok=True)
        elif not os.path.isdir(workdir):
            raise HTTPException(400, f"workdir tidak ada: {workdir}")

        run_id = store.create_run(
            body.goal,
            body.verify_cmd,
            workdir,
            model=body.model or cfg["claude"].get("model"),
            max_iterations=body.max_iterations or loops_cfg["max_iterations"],
            max_cost_usd=body.max_cost_usd or loops_cfg["max_cost_usd"],
        )
        return {"run_id": run_id, "status": "queued", "workdir": workdir}

    @app.get("/api/loops")
    def list_loops(request: Request) -> list[dict]:
        return request.app.state.store.list_runs()

    @app.get("/api/loops/{run_id}")
    def get_loop(run_id: str, request: Request) -> dict:
        store: Store = request.app.state.store
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run tidak ditemukan")
        run["iterations"] = store.iterations(run_id)
        return run

    @app.post("/api/loops/{run_id}/stop")
    def stop_loop(run_id: str, request: Request) -> dict:
        store: Store = request.app.state.store
        if store.get_run(run_id) is None:
            raise HTTPException(404, "run tidak ditemukan")
        store.request_stop(run_id)  # loop cek flag ini antar iterasi
        return {"run_id": run_id, "stop_requested": True}

    @app.get("/api/loops/{run_id}/events")
    async def stream_events(run_id: str, request: Request, after: int = 0):
        store: Store = request.app.state.store
        bus: EventBus = request.app.state.bus
        if store.get_run(run_id) is None:
            raise HTTPException(404, "run tidak ditemukan")

        async def gen():
            q = bus.subscribe(run_id)  # subscribe DULU baru replay → nggak ada gap
            try:
                last_id = after
                for ev in store.events_since(run_id, after_id=after):  # replay
                    last_id = ev["id"]
                    yield _sse(ev["id"], ev["type"], ev["payload"])

                if store.get_run(run_id)["status"] in TERMINAL:
                    yield "event: done\ndata: {}\n\n"
                    return

                while True:  # live
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_SEC)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if ev["id"] <= last_id:  # udah kekirim waktu replay
                        continue
                    last_id = ev["id"]
                    yield _sse(ev["id"], ev["type"], ev["payload"])
                    if ev["type"] == "status" and ev["payload"].get("status") in TERMINAL:
                        yield "event: done\ndata: {}\n\n"
                        return
            finally:
                bus.unsubscribe(run_id, q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx: jangan buffer SSE
        })

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/run/{run_id}")
    def run_page(run_id: str) -> FileResponse:
        # data di-fetch client-side pakai run_id dari URL
        return FileResponse(STATIC_DIR / "run.html")

    return app


def _sse(event_id: int, type_: str, payload: dict) -> str:
    return f"id: {event_id}\nevent: {type_}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


app = create_app()
