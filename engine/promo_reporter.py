"""Laporan traffic harian promo Buffer (attribution PostHog) -> Telegram.

Sengaja BUKAN lewat `schedules:` (yang nyepawn loop claude penuh) — laporan ini
murni ambil data + format teks, jadi task async ringan sendiri (pola sama kayak
scheduler.py/watchdog.py: next_at_delay buat jadwal harian "at: HH:MM" UTC),
konsisten sama prinsip resource frugality di CLAUDE.md: jangan bakar subscription
request buat kerjaan yang deterministik.

report_fn dibikin bisa di-inject (default: import scripts/promo_report.py via
importlib) biar gampang di-fake pas testing tanpa nyentuh network PostHog.

Teks laporan format Markdown (tabel pipe, bold, bullet) — di-convert ke HTML
Telegram lewat engine.telegram.md_to_tg_html (fungsi yang sama dipake chat agent),
jadi tabelnya kebentuk grid <pre> rapi, bukan blok teks polos.
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
            log.exception("promo_report gagal, skip kirim")
            return
        await self.bot.notify(md_to_tg_html(text))

    async def stop(self) -> None:
        self._stopping.set()

    async def _sleep(self, sec: float) -> bool:
        """Tidur responsif ke stop(). Return False kalau kepotong stop."""
        try:
            await asyncio.wait_for(self._stopping.wait(), sec)
            return False
        except TimeoutError:
            return True
