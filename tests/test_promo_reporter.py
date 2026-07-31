"""PromoReporter: sends the traffic report to Telegram — deterministic, no claude
subprocess. report_fn is faked, so nothing touches the real PostHog."""
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

def test_send_once_sends_via_bot_notify():
    bot = FakeBot()
    r = PromoReporter(bot, cfg(), report_fn=lambda days: f"report for {days} days")
    asyncio.run(r.send_once())
    assert len(bot.notified) == 1
    assert "report for 7 days" in bot.notified[0]


def test_send_once_uses_days_from_cfg():
    bot = FakeBot()
    captured = []
    r = PromoReporter(bot, cfg(days=3), report_fn=lambda days: captured.append(days) or "x")
    asyncio.run(r.send_once())
    assert captured == [3]


def test_send_once_converts_markdown_to_telegram_html():
    """send_once uses md_to_tg_html (not raw html.escape) — the table/bold/bullets from
    campaign_report come out neat (the same function is tested in test_telegram.py)."""
    bot = FakeBot()
    raw = "**Title**\n5 < 10 & profit > 0\n| a | b |\n|---|---|\n| 1 | 2 |"
    r = PromoReporter(bot, cfg(), report_fn=lambda days: raw)
    asyncio.run(r.send_once())
    assert bot.notified[0] == md_to_tg_html(raw)
    assert "<b>Title</b>" in bot.notified[0]
    assert "&lt; 10 &amp;" in bot.notified[0]


def test_send_once_report_fn_error_sends_nothing_and_does_not_crash():
    bot = FakeBot()

    def boom(days):
        raise RuntimeError("PostHog down")

    r = PromoReporter(bot, cfg(), report_fn=boom)
    asyncio.run(r.send_once())  # must not raise
    assert bot.notified == []


# ---- run_forever: bot/enabled gate ----

def test_run_forever_without_bot_finishes_immediately():
    r = PromoReporter(None, cfg(), report_fn=lambda days: "x")
    asyncio.run(asyncio.wait_for(r.run_forever(), timeout=1))


def test_run_forever_disabled_finishes_immediately():
    bot = FakeBot()
    r = PromoReporter(bot, cfg(enabled=False), report_fn=lambda days: "x")
    asyncio.run(asyncio.wait_for(r.run_forever(), timeout=1))
    assert bot.notified == []


# ---- run_forever: send_on_start + stop ----

def test_send_on_start_sends_before_waiting_for_the_schedule():
    async def scenario():
        bot = FakeBot()
        r = PromoReporter(bot, cfg(send_on_start=True, at="00:00"),
                          report_fn=lambda days: "hello")
        task = asyncio.create_task(r.run_forever())
        await asyncio.sleep(0.05)   # let send_on_start run before it sleeps until the schedule
        await r.stop()
        await asyncio.wait_for(task, timeout=1)
        assert len(bot.notified) == 1

    asyncio.run(scenario())


def test_stop_halts_the_loop_without_sending_again():
    async def scenario():
        bot = FakeBot()
        r = PromoReporter(bot, cfg(send_on_start=False), report_fn=lambda days: "hello")
        task = asyncio.create_task(r.run_forever())
        await asyncio.sleep(0.05)
        await r.stop()
        await asyncio.wait_for(task, timeout=1)
        assert bot.notified == []  # the next scheduled hour never arrived, stop won first

    asyncio.run(scenario())
