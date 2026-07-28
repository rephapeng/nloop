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
from pydantic import BaseModel, Field, model_validator

from engine import config, grounding, tasks, trace, triggers
from engine.events import EventBus
from engine.promo_reporter import PromoReporter
from engine.scheduler import Scheduler
from engine.store import Store
from engine.telegram import TelegramBot
from engine.watchdog import Watchdog
from engine.worker import Worker

STATIC_DIR = Path(__file__).parent / "static"
TERMINAL = ("succeeded", "failed", "stopped")
KEEPALIVE_SEC = 15


class LoopCreate(BaseModel):
    """Dua bentuk: `task` (+payload) dari registry, ATAU goal+verify_cmd ad-hoc."""
    task: str | None = None         # id task di registry (engine/tasks.py)
    payload: dict | None = None     # input template task
    idempotency_key: str | None = None  # 1 run aktif per key (kolom fingerprint)
    goal: str | None = None
    verify_cmd: str | None = None
    workdir: str | None = None      # default: workspaces/<id> dibikinin
    model: str | None = None
    max_iterations: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)
    role: str | None = None         # roles/<role>.md → system prompt
    context_cmd: str | None = None  # grounding segar tiap iterasi (stdout di-inject)
    gate_prompt: str | None = None  # kriteria LLM quality gate setelah verifier lolos

    @model_validator(mode="after")
    def _task_or_goal(self):
        if not self.task and not (self.goal and self.verify_cmd):
            raise ValueError("butuh 'task', atau pasangan 'goal' + 'verify_cmd'")
        return self


class TaskTrigger(BaseModel):
    """Body POST /api/tasks/{id}/trigger — payload + override seperlunya."""
    payload: dict = Field(default_factory=dict)
    idempotency_key: str | None = None
    workdir: str | None = None
    model: str | None = None
    max_iterations: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)

    def overrides(self) -> dict:
        return {k: getattr(self, k) for k in tasks.OVERRIDABLE}


def create_app(cfg: dict | None = None) -> FastAPI:
    cfg = cfg or config.load()
    cfg["tasks"] = tasks.load_registry(cfg)  # config.yaml + tasks/<id>.yaml

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
        promo_reporter = PromoReporter(bot, cfg)  # no-op sendiri kalau bot/enabled kosong
        worker_task = asyncio.create_task(worker.run_forever())
        sched_task = asyncio.create_task(scheduler.run_forever())
        wd_task = asyncio.create_task(watchdog.run_forever())
        pr_task = asyncio.create_task(promo_reporter.run_forever())
        bot_task = asyncio.create_task(bot.run_forever()) if bot else None
        app.state.store, app.state.bus, app.state.worker = store, bus, worker
        app.state.scheduler, app.state.bot = scheduler, bot
        app.state.watchdog = watchdog
        app.state.promo_reporter = promo_reporter
        yield
        if bot_task:                         # bot dulu (long-poll), baru worker
            bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
        if bot:
            await bot.stop()
        await promo_reporter.stop()
        await watchdog.stop()
        await scheduler.stop()
        await worker.stop()
        await asyncio.gather(worker_task, sched_task, wd_task, pr_task)

    app = FastAPI(title="nloop", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "app": "nloop"}

    @app.post("/api/loops", status_code=201)
    def create_loop(body: LoopCreate, request: Request) -> dict:
        store: Store = request.app.state.store
        loops_cfg = cfg["loops"]

        if body.task:  # jalur task registry — semua entry path ketemu di sini
            try:
                out = tasks.trigger(
                    store, cfg, body.task, body.payload,
                    idempotency_key=body.idempotency_key,
                    overrides={k: getattr(body, k) for k in tasks.OVERRIDABLE},
                )
            except tasks.TaskError as e:
                raise HTTPException(400, str(e))
            if out["deduped"]:  # run yang sama masih aktif → tunjuk yang itu
                return JSONResponse(status_code=200, content=out)
            return {"run_id": out["run_id"], "status": "queued",
                    "workdir": out["workdir"], "task": body.task,
                    "deduped": False}

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
    def list_loops(request: Request, task: str | None = None,
                   status: str | None = None, limit: int | None = None) -> list[dict]:
        return request.app.state.store.list_runs(task_id=task, status=status,
                                                 limit=limit)

    @app.get("/api/tasks")
    def list_tasks(request: Request) -> list[dict]:
        """Registry task + ringkasan run terakhir (dasar halaman Tasks)."""
        store: Store = request.app.state.store
        registry = cfg.get("tasks") or {}
        out = []
        for task_id, spec in registry.items():
            runs = store.list_runs(task_id=task_id, limit=5)
            item = tasks.summary(task_id, spec)
            item["triggerable"] = True
            item["runs_recent"] = runs
            item["last_run"] = runs[0] if runs else None
            out.append(item)
        # task bawaan (issue-fix dari webhook/watchdog) nggak ada di registry,
        # tapi run-nya ada — tetap ditampilin biar halaman Tasks nggak bohong.
        for row in store.task_ids():
            if row["task_id"] in registry:
                continue
            runs = store.list_runs(task_id=row["task_id"], limit=5)
            out.append({"id": row["task_id"], "name": row["task_id"],
                        "description": "built-in (webhook/watchdog)",
                        "triggerable": False, "required": [], "defaults": {},
                        "runs_recent": runs, "last_run": runs[0] if runs else None})
        return out

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, request: Request) -> dict:
        store: Store = request.app.state.store
        try:
            spec = tasks.get(cfg, task_id)
        except tasks.TaskError as e:
            runs = store.list_runs(task_id=task_id, limit=50)
            if not runs:  # bukan registry, nggak pernah jalan juga
                raise HTTPException(404, str(e))
            return {"id": task_id, "name": task_id, "triggerable": False,
                    "description": "built-in (webhook/watchdog)", "runs": runs}
        item = tasks.summary(task_id, spec)
        item["triggerable"] = True
        item["goal"] = spec.get("goal")
        item["gate_prompt"] = spec.get("gate_prompt")
        item["context_cmd"] = spec.get("context_cmd")
        item["on_success_cmd"] = spec.get("on_success_cmd")
        item["idempotency_key"] = spec.get("idempotency_key")
        item["runs"] = store.list_runs(task_id=task_id, limit=50)
        return item

    @app.post("/api/tasks/{task_id}/trigger", status_code=201)
    def trigger_task(task_id: str, body: TaskTrigger, request: Request):
        """Jalanin task dengan payload (pola tasks.trigger() trigger.dev)."""
        try:
            out = tasks.trigger(
                request.app.state.store, cfg, task_id, body.payload,
                idempotency_key=body.idempotency_key, overrides=body.overrides(),
            )
        except tasks.TaskError as e:
            code = 404 if "nggak ada di registry" in str(e) else 400
            raise HTTPException(code, str(e))
        if out["deduped"]:
            return JSONResponse(status_code=200, content=out)
        return out

    @app.get("/api/loops/{run_id}")
    def get_loop(run_id: str, request: Request) -> dict:
        store: Store = request.app.state.store
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run tidak ditemukan")
        run["iterations"] = store.iterations(run_id)
        return run

    @app.get("/api/loops/{run_id}/trace")
    def get_trace(run_id: str, request: Request) -> dict:
        """Span waterfall (Fase 12) — disusun dari iterations + events yang udah ada."""
        store: Store = request.app.state.store
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run tidak ditemukan")
        return trace.build(run, store.iterations(run_id), store.events_since(run_id))

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

    # Halaman: shell + data di-fetch client-side (vanilla JS, tanpa build step)
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/run/{run_id}")
    def run_page(run_id: str) -> FileResponse:
        # data di-fetch client-side pakai run_id dari URL
        return FileResponse(STATIC_DIR / "run.html")

    @app.get("/tasks")
    def tasks_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "tasks.html")

    @app.get("/tasks/{task_id}")
    def task_page(task_id: str) -> FileResponse:
        return FileResponse(STATIC_DIR / "task.html")

    @app.get("/schedules")
    def schedules_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "schedules.html")

    return app


def _sse(event_id: int, type_: str, payload: dict) -> str:
    return f"id: {event_id}\nevent: {type_}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


app = create_app()
