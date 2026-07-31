"""Scheduler: scheduled loops + sequential pipelines (a port of dtc-agent's systemd timers).

The `schedules:` config — each entry has `at: "HH:MM"` (daily, UTC — same as the dtc
timer) OR `every: "6h"` (an interval), plus `steps:`, a list of runs executed IN ORDER:
the next step only runs if the previous one succeeded, unless the step is marked
`always: true` (dtc's daily_pipeline pattern: the report still runs even when publishing
failed).

Dedup follows the webhook trigger pattern: a scheduled run is fingerprinted
`schedule:<name>`; if the next tick fires while the previous pipeline is still active
→ skip the tick (don't stack). Execution still goes through the worker + semaphore —
the scheduler only enqueues and waits.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from engine import config, tasks

log = logging.getLogger("nloop.scheduler")

TERMINAL = ("succeeded", "failed", "stopped")
_EVERY_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_AT_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
_UNIT_SEC = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_every(spec: str) -> float:
    m = _EVERY_RE.match(str(spec))
    if not m:
        raise ValueError(f"every '{spec}' is invalid (e.g. 30m, 6h, 1d)")
    return int(m.group(1)) * _UNIT_SEC[m.group(2)]


def next_at_delay(spec: str, now: float) -> float:
    """Seconds until the next occurrence of HH:MM (UTC)."""
    m = _AT_RE.match(str(spec))
    if not m:
        raise ValueError(f"at '{spec}' is invalid (e.g. \"01:04\", UTC)")
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        raise ValueError(f"at '{spec}' is out of clock range")
    t = time.gmtime(now)
    today_fire = now - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec) + hh * 3600 + mm * 60
    return today_fire - now if today_fire > now else today_fire + 86400 - now


def next_delay(spec: dict, now: float) -> float:
    if spec.get("at"):
        return next_at_delay(spec["at"], now)
    if spec.get("every"):
        return parse_every(spec["every"])
    raise ValueError("a schedule needs 'at' (HH:MM UTC) or 'every' (e.g. 6h)")


class Scheduler:
    def __init__(self, store, cfg: dict):
        self.store = store
        self.cfg = cfg
        self.workspace = cfg.get("workspace")
        self.poll = cfg.get("loops", {}).get("poll_interval_sec", 1.0)
        self._stopping = asyncio.Event()
        self._running: dict[str, tuple[dict, asyncio.Task]] = {}
        self._rejected: dict[str, dict] = {}   # name -> the spec we already complained about

    # ---- lifecycle ----

    async def run_forever(self) -> None:
        """Supervise one asyncio task per schedule, re-reading `schedules:` from the
        config files every config.RELOAD_INTERVAL_SEC.

        Editing config.yaml used to require `systemctl restart nloop`, which is a
        blunt instrument: a restart also drops SSE streams, requeues running loops,
        and kills the Telegram chat session. Only schedules whose spec actually
        changed are restarted — an untouched schedule keeps its timer.
        """
        self._sync(self.cfg.get("schedules") or {})
        while not self._stopping.is_set():
            if not await self._sleep(config.RELOAD_INTERVAL_SEC):
                break
            self._sync(self._from_disk())
        for _spec, task in self._running.values():
            task.cancel()
        await asyncio.gather(*(t for _s, t in self._running.values()),
                             return_exceptions=True)

    def _from_disk(self) -> dict:
        """`schedules:` re-read from the config files. A hand-built cfg (library use,
        tests) has no file behind it — keep whatever is in memory."""
        if not config.source_files(self.cfg):
            return self.cfg.get("schedules") or {}
        return config.section_from_disk(self.cfg, "schedules")

    def _sync(self, scheds: dict) -> None:
        """Reconcile running tasks against `scheds`: drop removed/changed ones, start
        new ones, leave untouched schedules alone."""
        wanted = {}
        for name, spec in (scheds or {}).items():
            # The supervisor re-reads config every 30s, so a broken spec would log on
            # every tick. Complain once per distinct spec instead.
            if self._rejected.get(name) == spec:
                continue
            if self._validate(name, spec):
                self._rejected[name] = spec
                continue
            self._rejected.pop(name, None)
            wanted[name] = spec

        for name in list(self._running):
            spec, task = self._running[name]
            if name in wanted and wanted[name] == spec:
                continue
            task.cancel()                  # an in-flight pipeline keeps running in the
            del self._running[name]        # worker; only the sequential wait stops here
            log.info("schedule '%s' %s", name,
                     "changed — restarting" if name in wanted else "removed")

        for name, spec in wanted.items():
            if name in self._running:
                continue
            self._running[name] = (
                spec, asyncio.create_task(self._run_schedule(name, spec)))
            log.info("schedule '%s' active", name)

        self.cfg["schedules"] = dict(wanted)   # keeps GET /api/schedules honest

    async def stop(self) -> None:
        self._stopping.set()

    def _validate(self, name: str, spec: dict) -> bool:
        """True = broken (skipped — a bad config must never take the server down)."""
        try:
            next_delay(spec, time.time())
            steps = self._steps(spec)
            if not steps:
                raise ValueError("no steps / goal / task")
            for i, step in enumerate(steps, start=1):
                if step.get("task"):
                    tasks.get(self.cfg, step["task"])  # missing task → skip the schedule
                elif not (step.get("goal") and step.get("verify_cmd")):
                    raise ValueError(f"step {i}: needs 'task' or goal+verify_cmd")
            return False
        except (ValueError, TypeError) as e:
            log.error("schedule '%s' is invalid, skipped: %s", name, e)
            return True

    # ---- execution ----

    async def _run_schedule(self, name: str, spec: dict) -> None:
        while not self._stopping.is_set():
            delay = next_delay(spec, time.time())
            if not await self._sleep(delay):
                return
            if self.store.find_active_by_fingerprint(f"schedule:{name}",
                                                     workspace=self.workspace):
                log.warning("schedule '%s': the previous tick is still active — skipping", name)
                continue
            try:
                await self.trigger(name, spec)
            except Exception:  # one blown-up schedule must not kill the scheduler
                log.exception("schedule '%s' error", name)

    async def trigger(self, name: str, spec: dict) -> list[str]:
        """Run the steps in order once (used by a tick and by the manual trigger endpoint)."""
        run_ids: list[str] = []
        prev_ok = True
        for i, step in enumerate(self._steps(spec), start=1):
            if not prev_ok and not step.get("always"):
                log.info("schedule '%s' step %d skipped (the previous step failed)", name, i)
                continue
            try:
                run_id = self._enqueue(name, step)
            except tasks.TaskError as e:  # missing payload etc. → step fails, pipeline continues
                log.error("schedule '%s' step %d failed to enqueue: %s", name, i, e)
                prev_ok = False
                continue
            run_ids.append(run_id)
            log.info("schedule '%s' step %d → run %s", name, i, run_id)
            status = await self._wait_terminal(run_id)
            prev_ok = status == "succeeded"
        return run_ids

    def _enqueue(self, name: str, step: dict) -> str:
        """Step → run. Two shapes: `task:` (+payload) from the registry, or inline.

        The fingerprint stays `schedule:<name>` (not the task's idempotency key) —
        the dedup that matters here is the schedule's: a new tick must not stack on
        top of a running pipeline. The fingerprint is not workspace-prefixed: every
        lookup is already scoped per workspace in the store, so two tenants may share
        a schedule name.
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
            workspace=self.workspace,
        )

    @staticmethod
    def _steps(spec: dict) -> list[dict]:
        """`steps: [...]`, or the short form: run/task fields directly on the spec."""
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
        """Sleep responsively to stop(). Returns False when cut short by stop."""
        try:
            await asyncio.wait_for(self._stopping.wait(), sec)
            return False
        except TimeoutError:
            return True
