#!/usr/bin/env python3
"""Laporan attribution promo Buffer -> PostHog, pake link UTM yang di-append
scripts/buffer_post.py (utm_source=twitter|threads, utm_campaign=pagi|sore|manual).

MarginIn (project 504239) sengaja nggak nyimpen personal API key PostHog di
.env-nya sendiri (cuma public project key buat kirim event) — key BUAT QUERY
disimpen di .env nloop ini (POSTHOG_PERSONAL_API_KEY), khusus dipake reporting
lintas-project kayak gini.

Pageview dari klik link UTM otomatis punya properti utm_source/utm_campaign
(di-parse PostHog JS SDK dari query string). Output-nya Markdown (tabel pipe +
bold + bullet) — scripts/promo_report.py dipake mentah di terminal, dan lewat
engine/promo_reporter.py di-convert ke HTML Telegram (engine.telegram.md_to_tg_html)
biar tabelnya kebentuk grid rapi kayak laporan bot lain.

Pemakaian:
    .venv/bin/python3 scripts/promo_report.py               # default 7 hari
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
PROJECT_ID = "504239"  # MarginIn — lihat lib/monitoring.ts (NEXT_PUBLIC_POSTHOG_KEY sama)
WIB = timezone(timedelta(hours=7))
PROMO_SOURCES = "'twitter', 'threads'"
TREND_POINTS = 7  # cap panjang rantai panah tren, berapa pun `days`-nya


def hogql(query: str) -> list:
    import httpx
    token = os.environ.get("POSTHOG_PERSONAL_API_KEY")
    if not token:
        sys.exit("POSTHOG_PERSONAL_API_KEY belum diset (isi di .env nloop)")
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


# ---------- helper tanggal WIB (pure, dites tanpa network) ----------

def wib_dates(n: int, end: datetime) -> list[str]:
    """n tanggal WIB ('YYYY-MM-DD') berturutan, LAMA -> BARU, berakhir di hari `end`."""
    end_date = end.astimezone(WIB).date()
    return [(end_date - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def fill_daily(rows: list, dates: list[str]) -> dict[str, tuple[int, int]]:
    """rows: [(date_str, pv, uniq), ...] dari HogQL -> dict lengkap (0-filled buat
    tanggal yang nggak punya event sama sekali, karena group-by ngilangin baris kosong)."""
    by_date = {str(d): (int(pv), int(uq)) for d, pv, uq in rows}
    return {d: by_date.get(d, (0, 0)) for d in dates}


def pct_change(today: int, yday: int) -> str:
    if yday == 0:
        return "baru" if today > 0 else "flat"
    delta = round((today - yday) / yday * 100)
    return f"+{delta}%" if delta >= 0 else f"{delta}%"


# ---------- queries ----------

def daily_series(days: int, source_filter: bool = False) -> list:
    """Marker -- daily_promo / -- daily_site: (date, pv, uniq) per hari WIB, N+2 hari
    ke belakang (buffer batas hari) — dipangkas presisi di Python via fill_daily()."""
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
    """Marker -- window_uniq_promo / -- window_uniq_site: distinct visitor SELURUH
    window sejak `since_date` (WIB, inklusif) — beda dari jumlah per-hari yang bisa
    dobel-hitung orang yang balik lagi. Filter tanggal WIB biar align sama site_week/
    promo_week (sum harian) di campaign_report, bukan rolling `now() - N hari`."""
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
    """Marker -- breakdown: (source, campaign, pv, uniq) per pasangan, sejak `since_date` WIB."""
    return hogql(f"""
        -- breakdown
        select properties.utm_source as source, properties.utm_campaign as campaign,
               count() as pv, count(distinct person_id) as uniq
        from events
        where event = '$pageview' and properties.utm_source in ({PROMO_SOURCES})
          and toDate(toTimeZone(timestamp, 'Asia/Jakarta')) >= '{since_date}'
        group by source, campaign order by source, campaign
    """)


FIRST_CLICK_LOOKBACK_DAYS = 90  # campaign UTM baru mulai — batasin scan biar ClickHouse murah


def first_promo_click() -> str | None:
    """Marker -- first_click: timestamp klik promo PERTAMA dalam FIRST_CLICK_LOOKBACK_DAYS
    hari terakhir (buat deteksi milestone), None kalau belum pernah ada. Sengaja DIBATASIN
    (bukan sepanjang masa) — tanpa batas timestamp, ClickHouse full-scan tabel events dan
    query-nya bisa 504 Gateway Timeout di PostHog."""
    rows = hogql(f"""
        -- first_click
        select min(timestamp) as t
        from events
        where event = '$pageview' and properties.utm_source in ({PROMO_SOURCES})
          and timestamp > now() - interval {FIRST_CLICK_LOOKBACK_DAYS} day
    """)
    return rows[0][0] if rows and rows[0][0] else None


# ---------- laporan ----------

def campaign_report(days: int = 7, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    dates = wib_dates(max(days, TREND_POINTS), now)
    today, yday = dates[-1], dates[-2]
    since = dates[-days]  # tanggal WIB `days` hari ke belakang (inklusif hari ini)

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
        f"📊 **Traffic MarginIn** — {tgl} (baru {jam} WIB)",
        "",
        "**🚦 Ringkasan**",
        "| Periode | Pageview | Unique | Klik Promo |",
        "|---|---|---|---|",
        f"| Hari ini | {pv_today} | {uq_today} | {promo_today} |",
        f"| Kemarin | {pv_yday} | {uq_yday} | {promo_yday_pv} |",
        f"| {days} hari | {site_week} | {site_uniq_week} | {promo_week} |",
        "",
    ]

    cmp_word = "ngelampauin" if pv_today > pv_yday else ("masih di bawah" if pv_today < pv_yday else "setara")
    lines.append(f"- Hari ini ({pv_today} pageview) {cmp_word} kemarin ({pv_yday}) — {pct_change(pv_today, pv_yday)}")

    trend = [site[d][0] for d in dates[-TREND_POINTS:]]
    lines.append(f"- Tren {len(trend)} hari (pageview situs): {' → '.join(str(v) for v in trend)}")

    if promo_week == 0:
        lines.append(f"- Belum ada klik dari post Twitter/Threads di {days} hari terakhir")
    else:
        lines.append(f"- Klik promo {days} hari: {promo_week} pageview, {promo_uniq_week} unique visitor"
                     + (f" ({promo_today} di antaranya hari ini)" if promo_today else ""))

    if breakdown:
        lines += ["", "**📣 Per campaign**", "| Source | Campaign | Pageview | Unique |", "|---|---|---|---|"]
        for source, campaign, pv, uniq in breakdown:
            lines.append(f"| {source} | {campaign} | {pv} | {uniq} |")

    if first_click:
        first_date = datetime.fromisoformat(str(first_click).replace("Z", "+00:00")).astimezone(WIB).date().isoformat()
        if first_date == today:
            lines += ["", "🏆 **MILESTONE: klik promo PERTAMA dari campaign ini!** 🎉"]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    load_env(str(ROOT / ".env"))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="window agregat/breakdown (default: 7)")
    ap.add_argument("--json", action="store_true", help="print breakdown mentah, bukan format rapi")
    args = ap.parse_args(argv)
    if args.json:
        since = wib_dates(args.days, datetime.now(timezone.utc))[0]
        print(json.dumps(campaign_breakdown(since), indent=2))
    else:
        print(campaign_report(args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
