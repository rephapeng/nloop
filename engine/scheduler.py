"""Scheduler: loop terjadwal + pipeline sekuensial (port systemd-timer dtc-agent).

Config `schedules:` — tiap entry punya `at: "HH:MM"` (harian, UTC — sama kayak
timer dtc) ATAU `every: "6h"` (interval), plus `steps:` daftar run yang jalan
BERURUTAN: step berikut cuma jalan kalau step sebelumnya succeeded, kecuali step
ditandai `always: true` (pola daily_pipeline dtc: report tetap jalan walau
publish gagal).

Dedup pola trigger webhook: run terjadwal di-fingerprint `schedule:<nama>`;
kalau tick berikutnya nyala pas pipeline lama masih aktif → skip tick (nggak
numpuk). Eksekusi run tetap lewat worker + semaphore — scheduler cuma enqueue
dan nunggu.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from engine import tasks

log = logging.getLogger("nloop.scheduler")

TERMINAL = ("succeeded", "failed", "stopped")
_EVERY_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_AT_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
_UNIT_SEC = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_every(spec: str) -> float:
    m = _EVERY_RE.match(str(spec))
    if not m:
        raise ValueError(f"every '{spec}' tidak valid (contoh: 30m, 6h, 1d)")
    return int(m.group(1)) * _UNIT_SEC[m.group(2)]


def next_at_delay(spec: str, now: float) -> float:
    """Detik sampai kemunculan HH:MM (UTC) berikutnya."""
    m = _AT_RE.match(str(spec))
    if not m:
        raise ValueError(f"at '{spec}' tidak valid (contoh: \"01:04\", UTC)")
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        raise ValueError(f"at '{spec}' di luar jangkauan jam")
    t = time.gmtime(now)
    today_fire = now - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec) + hh * 3600 + mm * 60
    return today_fire - now if today_fire > now else today_fire + 86400 - now


def next_delay(spec: dict, now: float) -> float:
    if spec.get("at"):
        return next_at_delay(spec["at"], now)
    if spec.get("every"):
        return parse_every(spec["every"])
    raise ValueError("schedule butuh 'at' (HH:MM UTC) atau 'every' (mis. 6h)")


class Scheduler:
    def __init__(self, store, cfg: dict):
        self.store = store
        self.cfg = cfg
        self.poll = cfg.get("loops", {}).get("poll_interval_sec", 1.0)
        self._stopping = asyncio.Event()

    # ---- lifecycle ----

    async def run_forever(self) -> None:
        scheds = self.cfg.get("schedules") or {}
        bad = [n for n, s in scheds.items() if self._validate(n, s)]
        tasks = [asyncio.create_task(self._run_schedule(name, spec))
                 for name, spec in scheds.items() if name not in bad]
        if tasks:
            log.info("scheduler: %d schedule aktif", len(tasks))
        await self._stopping.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._stopping.set()

    def _validate(self, name: str, spec: dict) -> bool:
        """True = rusak (di-skip, jangan matiin server gara-gara config)."""
        try:
            next_delay(spec, time.time())
            steps = self._steps(spec)
            if not steps:
                raise ValueError("tanpa steps / goal / task")
            for i, step in enumerate(steps, start=1):
                if step.get("task"):
                    tasks.get(self.cfg, step["task"])  # task nggak ada → skip schedule
                elif not (step.get("goal") and step.get("verify_cmd")):
                    raise ValueError(f"step {i}: butuh 'task' atau goal+verify_cmd")
            return False
        except (ValueError, TypeError) as e:
            log.error("schedule '%s' invalid, di-skip: %s", name, e)
            return True

    # ---- eksekusi ----

    async def _run_schedule(self, name: str, spec: dict) -> None:
        while not self._stopping.is_set():
            delay = next_delay(spec, time.time())
            if not await self._sleep(delay):
                return
            if self.store.find_active_by_fingerprint(f"schedule:{name}"):
                log.warning("schedule '%s': tick sebelumnya masih aktif — skip", name)
                continue
            try:
                await self.trigger(name, spec)
            except Exception:  # satu schedule meledak ≠ scheduler mati
                log.exception("schedule '%s' error", name)

    async def trigger(self, name: str, spec: dict) -> list[str]:
        """Jalankan steps berurutan sekali (dipakai tick & endpoint trigger manual)."""
        run_ids: list[str] = []
        prev_ok = True
        for i, step in enumerate(self._steps(spec), start=1):
            if not prev_ok and not step.get("always"):
                log.info("schedule '%s' step %d di-skip (step sebelumnya gagal)", name, i)
                continue
            try:
                run_id = self._enqueue(name, step)
            except tasks.TaskError as e:  # payload kurang dst. → step gagal, pipeline lanjut
                log.error("schedule '%s' step %d gagal di-enqueue: %s", name, i, e)
                prev_ok = False
                continue
            run_ids.append(run_id)
            log.info("schedule '%s' step %d → run %s", name, i, run_id)
            status = await self._wait_terminal(run_id)
            prev_ok = status == "succeeded"
        return run_ids

    def _enqueue(self, name: str, step: dict) -> str:
        """Step → run. Dua bentuk: `task:` (+payload) dari registry, atau inline.

        Fingerprint tetap `schedule:<nama>` (bukan idempotency key task-nya) —
        dedup schedule yang berlaku: tick baru nggak numpuk di atas pipeline lama.
        """
        loops_cfg = self.cfg["loops"]
        fingerprint = f"schedule:{name}"
        if step.get("task"):
            out = tasks.trigger(
                self.store, self.cfg, step["task"], step.get("payload"),
                idempotency_key=fingerprint,
                overrides={k: step.get(k) for k in tasks.OVERRIDABLE},
            )
            return out["run_id"]
        return self.store.create_run(
            step["goal"],
            step["verify_cmd"],
            step["workdir"],
            model=step.get("model") or self.cfg["claude"].get("model"),
            max_iterations=step.get("max_iterations") or loops_cfg["max_iterations"],
            max_cost_usd=step.get("max_cost_usd") or loops_cfg["max_cost_usd"],
            fingerprint=fingerprint,
            role=step.get("role"),
            context_cmd=step.get("context_cmd"),
            gate_prompt=step.get("gate_prompt"),
        )

    @staticmethod
    def _steps(spec: dict) -> list[dict]:
        """`steps: [...]` atau bentuk pendek: field run/task langsung di spec."""
        if spec.get("steps"):
            return list(spec["steps"])
        if spec.get("goal") or spec.get("task"):
            return [spec]
        return []

    async def _wait_terminal(self, run_id: str) -> str:
        while not self._stopping.is_set():
            run = self.store.get_run(run_id)
            if run and run["status"] in TERMINAL:
                return run["status"]
            await self._sleep(self.poll)
        return "stopped"

    async def _sleep(self, sec: float) -> bool:
        """Tidur responsif ke stop(). Return False kalau kepotong stop."""
        try:
            await asyncio.wait_for(self._stopping.wait(), sec)
            return False
        except TimeoutError:
            return True
