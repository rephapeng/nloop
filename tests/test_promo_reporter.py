"""PromoReporter: kirim laporan traffic ke Telegram — deterministik, no claude
subprocess. report_fn di-fake, jadi nggak nyentuh PostHog beneran."""
import asyncio

import pytest

from engine.promo_reporter import PromoReporter
from engine.telegram import md_to_tg_html


class FakeBot:
    def __init__(self):
        self.notified: list[str] = []

    async def notify(self, text: str) -> None:
        self.notified.append(text)


def cfg(**overrides):
    base = {"enabled": True, "at": "17:05", "days": 7, "send_on_start": False}
    base.update(overrides)
    return {"promo_report": base}


# ---- send_once ----

def test_send_once_kirim_via_bot_notify():
    bot = FakeBot()
    r = PromoReporter(bot, cfg(), report_fn=lambda days: f"laporan {days} hari")
    asyncio.run(r.send_once())
    assert len(bot.notified) == 1
    assert "laporan 7 hari" in bot.notified[0]


def test_send_once_pakai_days_dari_cfg():
    bot = FakeBot()
    captured = []
    r = PromoReporter(bot, cfg(days=3), report_fn=lambda days: captured.append(days) or "x")
    asyncio.run(r.send_once())
    assert captured == [3]


def test_send_once_convert_markdown_ke_html_telegram():
    """send_once make md_to_tg_html (bukan html.escape mentah) — tabel/bold/bullet
    dari campaign_report kebentuk rapi (fungsi yang sama dites di test_telegram.py)."""
    bot = FakeBot()
    raw = "**Judul**\n5 < 10 & untung > 0\n| a | b |\n|---|---|\n| 1 | 2 |"
    r = PromoReporter(bot, cfg(), report_fn=lambda days: raw)
    asyncio.run(r.send_once())
    assert bot.notified[0] == md_to_tg_html(raw)
    assert "<b>Judul</b>" in bot.notified[0]
    assert "&lt; 10 &amp;" in bot.notified[0]


def test_send_once_report_fn_error_gak_ngirim_dan_gak_crash():
    bot = FakeBot()

    def boom(days):
        raise RuntimeError("PostHog down")

    r = PromoReporter(bot, cfg(), report_fn=boom)
    asyncio.run(r.send_once())  # nggak boleh raise
    assert bot.notified == []


# ---- run_forever: gate bot/enabled ----

def test_run_forever_tanpa_bot_langsung_selesai():
    r = PromoReporter(None, cfg(), report_fn=lambda days: "x")
    asyncio.run(asyncio.wait_for(r.run_forever(), timeout=1))


def test_run_forever_disabled_langsung_selesai():
    bot = FakeBot()
    r = PromoReporter(bot, cfg(enabled=False), report_fn=lambda days: "x")
    asyncio.run(asyncio.wait_for(r.run_forever(), timeout=1))
    assert bot.notified == []


# ---- run_forever: send_on_start + stop ----

def test_send_on_start_kirim_sebelum_nunggu_jadwal():
    async def scenario():
        bot = FakeBot()
        r = PromoReporter(bot, cfg(send_on_start=True, at="00:00"),
                          report_fn=lambda days: "halo")
        task = asyncio.create_task(r.run_forever())
        await asyncio.sleep(0.05)   # kasih waktu send_on_start jalan sebelum masuk sleep jadwal
        await r.stop()
        await asyncio.wait_for(task, timeout=1)
        assert len(bot.notified) == 1

    asyncio.run(scenario())


def test_stop_menghentikan_loop_tanpa_kirim_lagi():
    async def scenario():
        bot = FakeBot()
        r = PromoReporter(bot, cfg(send_on_start=False), report_fn=lambda days: "halo")
        task = asyncio.create_task(r.run_forever())
        await asyncio.sleep(0.05)
        await r.stop()
        await asyncio.wait_for(task, timeout=1)
        assert bot.notified == []  # belum sampai jadwal jam berikutnya, stop duluan

    asyncio.run(scenario())
