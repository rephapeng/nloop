"""Worker: ambil run 'queued', jalanin loop, hormati semaphore MAX_CONCURRENT_LOOPS.

Krusial: tiap loop = pohon subprocess claude — semaphore ini guardrail resource
utama. Single-process by design: saat boot, semua run 'running' pasti orphan
dari proses sebelumnya (crash/restart) → di-requeue biar lanjut (tahan restart).
"""
from __future__ import annotations

import asyncio
import logging

from engine import loop

log = logging.getLogger("nloop.worker")


class Worker:
    def __init__(self, store, cfg: dict, on_event=None):
        """on_event(run_id, event_dict) — opsional, dipakai buat publish ke EventBus."""
        self.store = store
        self.cfg = cfg
        self.on_event = on_event
        loops_cfg = cfg.get("loops", {})
        self.max_concurrent: int = loops_cfg.get("max_concurrent", 2)
        self.poll_interval: float = loops_cfg.get("poll_interval_sec", 1.0)
        self.sem = asyncio.Semaphore(self.max_concurrent)
        self._stopping = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    async def run_forever(self) -> None:
        """Poll queue sampai stop(). Slot dulu (semaphore), baru claim run."""
        requeued = self.store.requeue_running()
        if requeued:
            log.info("requeue %d run orphan dari proses sebelumnya", requeued)

        while not self._stopping.is_set():
            await self.sem.acquire()
            if self._stopping.is_set():
                self.sem.release()
                break
            run_id = self.store.claim_queued()
            if run_id is None:
                self.sem.release()
                try:  # tidur sambil tetap responsif ke stop()
                    await asyncio.wait_for(self._stopping.wait(), self.poll_interval)
                except TimeoutError:
                    pass
                continue
            task = asyncio.create_task(self._run_one(run_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        if self._tasks:  # graceful: tunggu loop aktif kelar
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run_one(self, run_id: str) -> None:
        forward = (
            (lambda ev: self.on_event(run_id, ev)) if self.on_event else None
        )
        try:
            await loop.run_loop(run_id, self.store, self.cfg, on_event=forward)
        except Exception as exc:  # loop meledak ≠ worker mati
            log.exception("run %s error", run_id)
            self.store.finish(run_id, "failed")
            payload = {"status": "failed", "reason": f"worker_error: {exc}"}
            event_id = self.store.add_event(run_id, "status", payload)
            if self.on_event:  # SSE + notif Telegram juga harus tahu run mati
                self.on_event(run_id, {"id": event_id, "type": "status",
                                       "payload": payload})
        finally:
            self.sem.release()

    async def stop(self) -> None:
        """Berhenti ambil run baru, tunggu run aktif selesai. Run 'queued' tetap antri."""
        self._stopping.set()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
