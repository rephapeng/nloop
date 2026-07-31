"""Daily Buffer promo traffic report (PostHog attribution) -> Telegram.

Deliberately NOT routed through `schedules:` (which spawns a full claude loop) —
this report is pure data fetching + text formatting, so it is its own lightweight
async task (same pattern as scheduler.py/watchdog.py: next_at_delay for a daily
"at: HH:MM" UTC schedule). That is consistent with the resource-frugality principle
in CLAUDE.md: don't burn subscription requests on deterministic work.

report_fn is injectable (default: import scripts/promo_report.py via importlib) so
it is easy to fake in tests without touching the PostHog network.

The report text is Markdown (pipe tables, bold, bullets) — converted to Telegram
HTML by engine.telegram.md_to_tg_html (the same function the agent chat uses), so
the tables come out as a tidy <pre> grid instead of a flat block of text.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable

from engine.scheduler import next_at_delay
from engine.telegram import md_to_tg_html

log = logging.getLogger("nloop.promo_reporter")


def _default_report_fn(days: int) -> str:
    import importlib.util
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "promo_report", root / "scripts" / "promo_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.campaign_report(days)


class PromoReporter:
    def __init__(self, bot, cfg: dict, report_fn: Callable[[int], str] | None = None):
        self.bot = bot
        self.cfg = cfg.get("promo_report", {})
        self.report_fn = report_fn or _default_report_fn
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        if not self.bot or not self.cfg.get("enabled"):
            return
        if self.cfg.get("send_on_start"):
            await self.send_once()
        while not self._stopping.is_set():
            delay = next_at_delay(self.cfg.get("at", "17:05"), time.time())
            if not await self._sleep(delay):
                return
            await self.send_once()

    async def send_once(self) -> None:
        try:
            text = await asyncio.to_thread(self.report_fn, self.cfg.get("days", 7))
        except Exception:
            log.exception("promo_report failed, skipping send")
            return
        await self.bot.notify(md_to_tg_html(text))

    async def stop(self) -> None:
        self._stopping.set()

    async def _sleep(self, sec: float) -> bool:
        """Sleep responsively to stop(). Returns False when cut short by stop."""
        try:
            await asyncio.wait_for(self._stopping.wait(), sec)
            return False
        except TimeoutError:
            return True
