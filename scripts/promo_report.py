#!/usr/bin/env python3
"""Buffer -> PostHog promo attribution report, using the UTM links that
scripts/buffer_post.py appends (utm_source=twitter|threads, utm_campaign=pagi|sore|manual).

MarginIn (project 504239) deliberately does NOT keep a PostHog personal API key in its
own .env (only the public project key, for sending events) — the key FOR QUERYING lives
in this nloop .env (POSTHOG_PERSONAL_API_KEY), used only for cross-project reporting
like this.

A pageview from a UTM link click automatically carries utm_source/utm_campaign
properties (the PostHog JS SDK parses them off the query string). Output is Markdown
(pipe table + bold + bullets) — scripts/promo_report.py is used raw in the terminal, and
via engine/promo_reporter.py it gets converted to Telegram HTML
(engine.telegram.md_to_tg_html) so the table renders as a neat grid like the other bot
reports.

Usage:
    .venv/bin/python3 scripts/promo_report.py               # default 7 days
    .venv/bin/python3 scripts/promo_report.py --days 14
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import load_env  # noqa: E402

POSTHOG_HOST = "https://us.i.posthog.com"
PROJECT_ID = "504239"  # MarginIn — see lib/monitoring.ts (same NEXT_PUBLIC_POSTHOG_KEY)
WIB = timezone(timedelta(hours=7))
PROMO_SOURCES = "'twitter', 'threads'"
TREND_POINTS = 7  # cap on the trend arrow chain, no matter how big `days` is


def hogql(query: str) -> list:
    import httpx
    token = os.environ.get("POSTHOG_PERSONAL_API_KEY")
    if not token:
        sys.exit("POSTHOG_PERSONAL_API_KEY is not set (put it in the nloop .env)")
    r = httpx.post(
        f"{POSTHOG_HOST}/api/projects/{PROJECT_ID}/query/",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": {"kind": "HogQLQuery", "query": query}},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"PostHog query error: {data['error']}")
    return data.get("results") or []


# ---------- WIB date helpers (pure, tested without network) ----------

def wib_dates(n: int, end: datetime) -> list[str]:
    """n consecutive WIB dates ('YYYY-MM-DD'), OLD -> NEW, ending on day `end`."""
    end_date = end.astimezone(WIB).date()
    return [(end_date - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def fill_daily(rows: list, dates: list[str]) -> dict[str, tuple[int, int]]:
    """rows: [(date_str, pv, uniq), ...] from HogQL -> a complete dict (0-filled for the
    dates with no events at all, since a group-by drops empty rows entirely)."""
    by_date = {str(d): (int(pv), int(uq)) for d, pv, uq in rows}
    return {d: by_date.get(d, (0, 0)) for d in dates}


def pct_change(today: int, yday: int) -> str:
    if yday == 0:
        return "new" if today > 0 else "flat"
    delta = round((today - yday) / yday * 100)
    return f"+{delta}%" if delta >= 0 else f"{delta}%"


# ---------- queries ----------

def daily_series(days: int, source_filter: bool = False) -> list:
    """Marker -- daily_promo / -- daily_site: (date, pv, uniq) per WIB day, N+2 days back
    (buffer for the day boundary) — trimmed precisely in Python via fill_daily()."""
    marker = "-- daily_promo" if source_filter else "-- daily_site"
    extra = f"and properties.utm_source in ({PROMO_SOURCES})" if source_filter else ""
    return hogql(f"""
        {marker}
        select toDate(toTimeZone(timestamp, 'Asia/Jakarta')) as day,
               count() as pv, count(distinct person_id) as uniq
        from events
        where event = '$pageview' {extra}
          and timestamp > now() - interval {days + 2} day
        group by day order by day
    """)


def window_uniques(since_date: str, source_filter: bool = False) -> int:
    """Marker -- window_uniq_promo / -- window_uniq_site: distinct visitors across the
    WHOLE window since `since_date` (WIB, inclusive) — different from summing the per-day
    counts, which double-counts anyone who comes back. Filtering on the WIB date keeps it
    aligned with site_week/promo_week (daily sums) in campaign_report, instead of a
    rolling `now() - N days`."""
    marker = "-- window_uniq_promo" if source_filter else "-- window_uniq_site"
    extra = f"and properties.utm_source in ({PROMO_SOURCES})" if source_filter else ""
    rows = hogql(f"""
        {marker}
        select count(distinct person_id) as n
        from events
        where event = '$pageview' {extra}
          and toDate(toTimeZone(timestamp, 'Asia/Jakarta')) >= '{since_date}'
    """)
    return int(rows[0][0]) if rows else 0


def campaign_breakdown(since_date: str) -> list:
    """Marker -- breakdown: (source, campaign, pv, uniq) per pair, since `since_date` WIB."""
    return hogql(f"""
        -- breakdown
        select properties.utm_source as source, properties.utm_campaign as campaign,
               count() as pv, count(distinct person_id) as uniq
        from events
        where event = '$pageview' and properties.utm_source in ({PROMO_SOURCES})
          and toDate(toTimeZone(timestamp, 'Asia/Jakarta')) >= '{since_date}'
        group by source, campaign order by source, campaign
    """)


FIRST_CLICK_LOOKBACK_DAYS = 90  # UTM campaigns are brand new — cap the scan, keep it cheap


def first_promo_click() -> str | None:
    """Marker -- first_click: timestamp of the FIRST promo click within the last
    FIRST_CLICK_LOOKBACK_DAYS days (for milestone detection), None if there never was one.
    Deliberately CAPPED (not all-time) — without a timestamp bound ClickHouse full-scans
    the events table and the query can come back as a 504 Gateway Timeout from PostHog."""
    rows = hogql(f"""
        -- first_click
        select min(timestamp) as t
        from events
        where event = '$pageview' and properties.utm_source in ({PROMO_SOURCES})
          and timestamp > now() - interval {FIRST_CLICK_LOOKBACK_DAYS} day
    """)
    return rows[0][0] if rows and rows[0][0] else None


# ---------- report ----------

def campaign_report(days: int = 7, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    dates = wib_dates(max(days, TREND_POINTS), now)
    today, yday = dates[-1], dates[-2]
    since = dates[-days]  # the WIB date `days` days back (today inclusive)

    site = fill_daily(daily_series(len(dates), source_filter=False), dates)
    promo = fill_daily(daily_series(len(dates), source_filter=True), dates)
    site_week = sum(pv for pv, _ in list(site.values())[-days:])
    promo_week = sum(pv for pv, _ in list(promo.values())[-days:])
    site_uniq_week = window_uniques(since, source_filter=False)
    promo_uniq_week = window_uniques(since, source_filter=True)
    breakdown = campaign_breakdown(since)
    first_click = first_promo_click()

    pv_today, uq_today = site[today]
    pv_yday, uq_yday = site[yday]
    promo_today, promo_uq_today = promo[today]
    promo_yday_pv, _ = promo[yday]

    jam = now.astimezone(WIB).strftime("%H:%M")
    tgl = now.astimezone(WIB).strftime("%d %b %Y")

    lines = [
        f"📊 **MarginIn Traffic** — {tgl} (as of {jam} WIB)",
        "",
        "**🚦 Summary**",
        "| Period | Pageviews | Unique | Promo clicks |",
        "|---|---|---|---|",
        f"| Today | {pv_today} | {uq_today} | {promo_today} |",
        f"| Yesterday | {pv_yday} | {uq_yday} | {promo_yday_pv} |",
        f"| {days} days | {site_week} | {site_uniq_week} | {promo_week} |",
        "",
    ]

    cmp_word = ("beat" if pv_today > pv_yday
                else "is still under" if pv_today < pv_yday else "matches")
    lines.append(f"- Today ({pv_today} pageviews) {cmp_word} yesterday ({pv_yday})"
                 f" — {pct_change(pv_today, pv_yday)}")

    trend = [site[d][0] for d in dates[-TREND_POINTS:]]
    arrows = ' → '.join(str(v) for v in trend)
    lines.append(f"- {len(trend)}-day trend (site pageviews): {arrows}")

    if promo_week == 0:
        lines.append(f"- No clicks from Twitter/Threads posts in the last {days} days")
    else:
        lines.append(f"- Promo clicks over {days} days: {promo_week} pageviews, "
                     f"{promo_uniq_week} unique visitors"
                     + (f" ({promo_today} of them today)" if promo_today else ""))

    if breakdown:
        lines += ["", "**📣 Per campaign**", "| Source | Campaign | Pageviews | Unique |", "|---|---|---|---|"]
        for source, campaign, pv, uniq in breakdown:
            lines.append(f"| {source} | {campaign} | {pv} | {uniq} |")

    if first_click:
        first_date = datetime.fromisoformat(str(first_click).replace("Z", "+00:00")).astimezone(WIB).date().isoformat()
        if first_date == today:
            lines += ["", "🏆 **MILESTONE: the FIRST promo click of this campaign!** 🎉"]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    load_env(str(ROOT / ".env"))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7,
                    help="aggregate/breakdown window (default: 7)")
    ap.add_argument("--json", action="store_true",
                    help="print the raw breakdown instead of the formatted report")
    args = ap.parse_args(argv)
    if args.json:
        since = wib_dates(args.days, datetime.now(timezone.utc))[0]
        print(json.dumps(campaign_breakdown(since), indent=2))
    else:
        print(campaign_report(args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
