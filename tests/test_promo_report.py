"""scripts/promo_report.py: helper tanggal WIB (pure) + query builder (hogql
di-monkeypatch) + campaign_report composite (sub-query di-monkeypatch) —
semuanya tanpa network ke PostHog beneran."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "promo_report", Path(__file__).parent.parent / "scripts" / "promo_report.py")
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)

WIB = pr.WIB


def wib(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=WIB)


# ---- wib_dates / fill_daily / pct_change (pure) ----

def test_wib_dates_urut_lama_ke_baru():
    end = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)  # 17:00 WIB
    assert pr.wib_dates(3, end) == ["2026-07-15", "2026-07-16", "2026-07-17"]


def test_wib_dates_lewat_tengah_malam_wib():
    end = datetime(2026, 7, 16, 17, 30, tzinfo=timezone.utc)  # 00:30 WIB tgl 17
    assert pr.wib_dates(1, end) == ["2026-07-17"]


def test_fill_daily_nol_kan_tanggal_kosong():
    rows = [("2026-07-16", 10, 8)]
    dates = ["2026-07-15", "2026-07-16", "2026-07-17"]
    out = pr.fill_daily(rows, dates)
    assert out == {"2026-07-15": (0, 0), "2026-07-16": (10, 8), "2026-07-17": (0, 0)}


def test_pct_change():
    assert pr.pct_change(15, 10) == "+50%"
    assert pr.pct_change(5, 10) == "-50%"
    assert pr.pct_change(10, 10) == "+0%"
    assert pr.pct_change(5, 0) == "baru"
    assert pr.pct_change(0, 0) == "flat"


# ---- query builder: marker + filter/since_date kebentuk bener ----

def test_daily_series_marker_dan_filter(monkeypatch):
    captured = []
    monkeypatch.setattr(pr, "hogql", lambda q: (captured.append(q), [])[1])
    pr.daily_series(5, source_filter=False)
    assert captured[0].strip().startswith("-- daily_site")
    assert "utm_source" not in captured[0]

    pr.daily_series(5, source_filter=True)
    assert captured[1].strip().startswith("-- daily_promo")
    assert "utm_source in ('twitter', 'threads')" in captured[1]


def test_window_uniques_pakai_since_date(monkeypatch):
    captured = []
    monkeypatch.setattr(pr, "hogql", lambda q: (captured.append(q), [[42]])[1])
    n = pr.window_uniques("2026-07-10", source_filter=True)
    assert n == 42
    assert "-- window_uniq_promo" in captured[0]
    assert "'2026-07-10'" in captured[0]


def test_campaign_breakdown_since_date(monkeypatch):
    captured = []
    monkeypatch.setattr(pr, "hogql", lambda q: (captured.append(q), [])[1])
    pr.campaign_breakdown("2026-07-10")
    assert "-- breakdown" in captured[0]
    assert "'2026-07-10'" in captured[0]


def test_first_promo_click_marker(monkeypatch):
    monkeypatch.setattr(pr, "hogql", lambda q: [["2026-07-17T01:00:00Z"]])
    assert pr.first_promo_click() == "2026-07-17T01:00:00Z"


def test_first_promo_click_kosong(monkeypatch):
    monkeypatch.setattr(pr, "hogql", lambda q: [])
    assert pr.first_promo_click() is None


# ---- campaign_report: composite, semua sub-query di-monkeypatch ----

NOW = wib(2026, 7, 17, 21, 30)  # laporan sore, "hari ini" = 17 Jul


def patch_report(monkeypatch, *, site_rows, promo_rows, site_uniq=0, promo_uniq=0,
                 breakdown=None, first_click=None):
    monkeypatch.setattr(pr, "daily_series",
                        lambda days, source_filter=False: promo_rows if source_filter else site_rows)
    monkeypatch.setattr(pr, "window_uniques",
                        lambda since, source_filter=False: promo_uniq if source_filter else site_uniq)
    monkeypatch.setattr(pr, "campaign_breakdown", lambda since: breakdown or [])
    monkeypatch.setattr(pr, "first_promo_click", lambda: first_click)


def test_report_tanpa_klik_promo(monkeypatch):
    site_rows = [("2026-07-16", 20, 15), ("2026-07-17", 25, 18)]
    patch_report(monkeypatch, site_rows=site_rows, promo_rows=[], site_uniq=100)
    out = pr.campaign_report(days=7, now=NOW)
    assert "Hari ini | 25 | 18 | 0" in out
    assert "Kemarin | 20 | 15 | 0" in out
    assert "Belum ada klik dari post Twitter/Threads di 7 hari terakhir" in out
    assert "MILESTONE" not in out


def test_report_dengan_klik_dan_breakdown(monkeypatch):
    site_rows = [("2026-07-16", 20, 15), ("2026-07-17", 25, 18)]
    promo_rows = [("2026-07-17", 5, 4)]
    breakdown = [["twitter", "sore", 3, 2], ["threads", "sore", 2, 2]]
    patch_report(monkeypatch, site_rows=site_rows, promo_rows=promo_rows,
                 site_uniq=100, promo_uniq=4, breakdown=breakdown)
    out = pr.campaign_report(days=7, now=NOW)
    assert "Hari ini | 25 | 18 | 5" in out
    assert "twitter | sore | 3 | 2" in out
    assert "threads | sore | 2 | 2" in out
    assert "Klik promo 7 hari: 5 pageview, 4 unique visitor (5 di antaranya hari ini)" in out


def test_report_trend_line_urut_dan_dicap_7_titik(monkeypatch):
    dates = ["2026-07-11", "2026-07-12", "2026-07-13", "2026-07-14",
             "2026-07-15", "2026-07-16", "2026-07-17"]
    site_rows = [(d, i + 1, i + 1) for i, d in enumerate(dates)]
    patch_report(monkeypatch, site_rows=site_rows, promo_rows=[])
    out = pr.campaign_report(days=7, now=NOW)
    assert "Tren 7 hari (pageview situs): 1 → 2 → 3 → 4 → 5 → 6 → 7" in out


def test_report_milestone_klik_pertama_hari_ini(monkeypatch):
    patch_report(monkeypatch, site_rows=[("2026-07-17", 10, 8)],
                 promo_rows=[("2026-07-17", 1, 1)],
                 first_click="2026-07-17T05:00:00Z")
    out = pr.campaign_report(days=7, now=NOW)
    assert "MILESTONE" in out


def test_report_bukan_milestone_kalau_klik_pertama_bukan_hari_ini(monkeypatch):
    patch_report(monkeypatch, site_rows=[("2026-07-17", 10, 8)],
                 promo_rows=[("2026-07-17", 1, 1)],
                 first_click="2026-07-10T05:00:00Z")
    out = pr.campaign_report(days=7, now=NOW)
    assert "MILESTONE" not in out


def test_report_tanpa_first_click_sama_sekali_gak_meledak(monkeypatch):
    patch_report(monkeypatch, site_rows=[("2026-07-17", 10, 8)], promo_rows=[])
    out = pr.campaign_report(days=7, now=NOW)
    assert "MILESTONE" not in out
