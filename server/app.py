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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine import config, grounding, triggers
from engine.events import EventBus
from engine.scheduler import Scheduler
from engine.store import Store
from engine.telegram import TelegramBot
from engine.watchdog import Watchdog
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
    role: str | None = None         # roles/<role>.md → system prompt
    context_cmd: str | None = None  # grounding segar tiap iterasi (stdout di-inject)
    gate_prompt: str | None = None  # kriteria LLM quality gate setelah verifier lolos


def create_app(cfg: dict | None = None) -> FastAPI:
    cfg = cfg or config.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config.load_env()                    # secrets (.env) — token Telegram dst.
        store = Store(cfg["paths"]["db"])
        bus = EventBus()

        bot: TelegramBot | None = None
        if cfg["telegram"].get("enabled") and os.environ.get("TELEGRAM_BOT_TOKEN"):
            bot = TelegramBot(cfg, store)

        def on_event(run_id: str, ev: dict) -> None:
            bus.publish(run_id, ev)
            # notif Telegram saat run mencapai status final
            if (bot and cfg["telegram"].get("notify", True)
                    and ev["type"] == "status"
                    and ev["payload"].get("status") in TERMINAL):
                run = store.get_run(run_id)
                if run:
                    asyncio.create_task(bot.notify_run_finished(run, ev["payload"]))

        worker = Worker(store, cfg, on_event=on_event)
        scheduler = Scheduler(store, cfg)
        watchdog = Watchdog(store, cfg)
        worker_task = asyncio.create_task(worker.run_forever())
        sched_task = asyncio.create_task(scheduler.run_forever())
        wd_task = asyncio.create_task(watchdog.run_forever())
        bot_task = asyncio.create_task(bot.run_forever()) if bot else None
        app.state.store, app.state.bus, app.state.worker = store, bus, worker
        app.state.scheduler, app.state.bot = scheduler, bot
        app.state.watchdog = watchdog
        yield
        if bot_task:                         # bot dulu (long-poll), baru worker
            bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
        if bot:
            await bot.stop()
        await watchdog.stop()
        await scheduler.stop()
        await worker.stop()
        await asyncio.gather(worker_task, sched_task, wd_task)

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

        if body.role:  # fail cepat di sini, bukan pas run udah jalan
            try:
                grounding.role_prompt(cfg, body.role)
            except ValueError as e:
                raise HTTPException(400, str(e))

        run_id = store.create_run(
            body.goal,
            body.verify_cmd,
            workdir,
            model=body.model or cfg["claude"].get("model"),
            max_iterations=body.max_iterations or loops_cfg["max_iterations"],
            max_cost_usd=body.max_cost_usd or loops_cfg["max_cost_usd"],
            role=body.role,
            context_cmd=body.context_cmd,
            gate_prompt=body.gate_prompt,
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

    @app.post("/api/hooks/{source}", status_code=201)
    async def webhook(source: str, request: Request, project: str,
                      token: str | None = None):
        """Sentry/PostHog/generic webhook → spawn loop (dedup per fingerprint)."""
        trig = cfg.get("triggers", {})
        if trig.get("token") and token != trig["token"]:
            raise HTTPException(401, "token salah")
        proj = (trig.get("projects") or {}).get(project)
        if proj is None:
            raise HTTPException(404, f"project '{project}' tidak terdaftar di triggers.projects")
        if not os.path.isdir(proj.get("workdir", "")):
            raise HTTPException(500, f"workdir project '{project}' tidak ada")

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(400, "payload bukan JSON valid")
        issue = triggers.extract_issue(source, payload if isinstance(payload, dict) else {})

        store: Store = request.app.state.store
        existing = store.find_active_by_fingerprint(issue["fingerprint"])
        if existing:  # issue sama masih dikerjain → jangan spawn dobel
            return JSONResponse(status_code=200, content={
                "run_id": existing, "deduped": True,
                "fingerprint": issue["fingerprint"],
            })

        # Repro-first + spawn: jalur bersama dengan watchdog (triggers.create_issue_run)
        run_id = triggers.create_issue_run(store, cfg, proj, source, issue)
        return {"run_id": run_id, "deduped": False,
                "fingerprint": issue["fingerprint"], "title": issue["title"]}

    @app.get("/api/schedules")
    def list_schedules(request: Request) -> dict:
        store: Store = request.app.state.store
        out = {}
        for name, spec in (cfg.get("schedules") or {}).items():
            out[name] = {
                "at": spec.get("at"), "every": spec.get("every"),
                "steps": len(Scheduler._steps(spec)),
                "active_run": store.find_active_by_fingerprint(f"schedule:{name}"),
            }
        return out

    @app.post("/api/schedules/{name}/trigger", status_code=202)
    async def trigger_schedule(name: str, request: Request) -> dict:
        """Jalankan pipeline schedule SEKARANG (setara `systemctl start` timer dtc)."""
        spec = (cfg.get("schedules") or {}).get(name)
        if spec is None:
            raise HTTPException(404, f"schedule '{name}' tidak ada")
        store: Store = request.app.state.store
        active = store.find_active_by_fingerprint(f"schedule:{name}")
        if active:
            return {"triggered": False, "reason": "masih aktif", "run_id": active}
        scheduler: Scheduler = request.app.state.scheduler
        asyncio.create_task(scheduler.trigger(name, spec))
        return {"triggered": True, "schedule": name}

    @app.get("/api/watchdog")
    def watchdog_status(request: Request) -> dict:
        return request.app.state.watchdog.status()

    @app.post("/api/watchdog/tick", status_code=202)
    async def watchdog_tick(request: Request) -> dict:
        """Paksa satu putaran poll SEKARANG (tanpa nunggu interval)."""
        w = cfg.get("watchdog", {})
        if not w.get("enabled") or not w.get("organization"):
            raise HTTPException(400, "watchdog belum dikonfigurasi (enabled + organization)")
        spawned = await request.app.state.watchdog.tick()
        return {"spawned": spawned}

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
