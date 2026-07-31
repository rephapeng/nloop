"""Worker: claim 'queued' runs, drive the loop, respect the MAX_CONCURRENT_LOOPS semaphore.

Crucial: each loop is a whole tree of claude subprocesses — this semaphore is the
main resource guardrail. Single-process by design: at boot, every 'running' row is
necessarily an orphan from the previous process (crash/restart) → requeued so it
carries on (restart-tolerant).
"""
from __future__ import annotations

import asyncio
import logging

from engine import loop

log = logging.getLogger("nloop.worker")


class Worker:
    def __init__(self, store, cfg: dict, on_event=None):
        """on_event(run_id, event_dict) — optional, used to publish to the EventBus."""
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
        """Poll the queue until stop(). Take a slot (semaphore) first, then claim a run."""
        requeued = self.store.requeue_running()
        if requeued:
            log.info("requeued %d orphan run(s) from the previous process", requeued)

        while not self._stopping.is_set():
            await self.sem.acquire()
            if self._stopping.is_set():
                self.sem.release()
                break
            run_id = self.store.claim_queued()
            if run_id is None:
                self.sem.release()
                try:  # sleep while staying responsive to stop()
                    await asyncio.wait_for(self._stopping.wait(), self.poll_interval)
                except TimeoutError:
                    pass
                continue
            task = asyncio.create_task(self._run_one(run_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        if self._tasks:  # graceful: wait for active loops to finish
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run_one(self, run_id: str) -> None:
        forward = (
            (lambda ev: self.on_event(run_id, ev)) if self.on_event else None
        )
        try:
            await loop.run_loop(run_id, self.store, self.cfg, on_event=forward)
        except Exception as exc:  # a blown-up loop must not kill the worker
            log.exception("run %s error", run_id)
            self.store.finish(run_id, "failed")
            payload = {"status": "failed", "reason": f"worker_error: {exc}"}
            event_id = self.store.add_event(run_id, "status", payload)
            if self.on_event:  # SSE + Telegram notifications must learn the run died too
                self.on_event(run_id, {"id": event_id, "type": "status",
                                       "payload": payload})
        finally:
            self.sem.release()

    async def stop(self) -> None:
        """Stop claiming new runs, wait for active ones. 'queued' runs stay queued."""
        self._stopping.set()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
