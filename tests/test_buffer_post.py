"""scripts/buffer_post.py: helper murni + verify_report — tanpa network.

Script-nya bukan package, jadi diimport via importlib. Fungsi yang nyentuh
API Buffer (gql/fetch_posts) nggak dites di sini — yang dites logika slot WIB,
validasi konten (twitter wajib hashtag, threads wajib topic), dan verifier.
"""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "buffer_post", Path(__file__).parent.parent / "scripts" / "buffer_post.py")
bp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bp)

WIB = bp.WIB


def wib(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=WIB)


# ---- next_slot_due ----

def test_next_slot_pagi_masih_jauh():
    # 05:00 WIB -> slot pagi hari yang sama 07:30 WIB
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0))
    assert due.astimezone(WIB) == wib(2026, 7, 17, 7, 30)


def test_next_slot_kelewat_geser_besok():
    # 08:00 WIB (07:30 udah lewat) -> besok
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 8, 0))
    assert due.astimezone(WIB) == wib(2026, 7, 18, 7, 30)


def test_next_slot_mepet_kurang_dari_lead_geser_besok():
    # 07:25 WIB cuma 5 menit sebelum slot (< MIN_LEAD_MIN) -> besok
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 7, 25))
    assert due.astimezone(WIB) == wib(2026, 7, 18, 7, 30)


def test_next_slot_sore():
    due = bp.next_slot_due("sore", wib(2026, 7, 17, 10, 0))
    assert due.astimezone(WIB) == wib(2026, 7, 17, 19, 0)


def test_next_slot_hasilnya_utc():
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0))
    assert due.tzinfo == timezone.utc
    assert due.hour == 0 and due.minute == 30  # 07:30 WIB = 00:30 UTC


# ---- in_slot_window ----

def test_window_pagi():
    assert bp.in_slot_window("pagi", wib(2026, 7, 17, 7, 30))
    assert bp.in_slot_window("pagi", wib(2026, 7, 17, 5, 30))   # batas bawah inklusif
    assert bp.in_slot_window("pagi", wib(2026, 7, 17, 10, 30))  # batas atas inklusif
    assert not bp.in_slot_window("pagi", wib(2026, 7, 17, 11, 0))
    assert not bp.in_slot_window("pagi", wib(2026, 7, 17, 19, 0))  # jam sore


def test_window_pake_jam_wib_bukan_utc():
    # 00:30 UTC = 07:30 WIB -> masuk window pagi walau jam UTC-nya subuh
    assert bp.in_slot_window("pagi", datetime(2026, 7, 17, 0, 30, tzinfo=timezone.utc))


# ---- validate_text ----

def test_twitter_wajib_hashtag():
    assert bp.validate_text("twitter", "promosi tanpa tagar")
    assert not bp.validate_text("twitter", "promosi #UMKM")


def test_batas_panjang():
    assert bp.validate_text("twitter", "#UMKM " + "x" * 280)
    assert not bp.validate_text("twitter", "#UMKM " + "x" * 270)
    assert bp.validate_text("threads", "x" * 501)
    assert not bp.validate_text("threads", "x" * 500)


def test_teks_kosong_ditolak():
    assert bp.validate_text("twitter", "   ")


# ---- build_create_input ----

def test_threads_dapet_topic_default():
    inp = bp.build_create_input("ch1", "threads", "halo", "2026-07-17T00:30:00.000Z", None)
    assert inp["metadata"]["threads"]["topic"] == bp.DEFAULT_TOPIC
    assert inp["mode"] == "customScheduled"
    assert inp["schedulingType"] == "automatic"
    assert inp["assets"] == []


def test_threads_topic_custom():
    inp = bp.build_create_input("ch1", "threads", "halo", "t", "UMKMthreads")
    assert inp["metadata"]["threads"]["topic"] == "UMKMthreads"


def test_twitter_tanpa_metadata():
    inp = bp.build_create_input("ch1", "twitter", "halo #UMKM", "t", None)
    assert "metadata" not in inp


def test_due_none_jadi_share_now():
    inp = bp.build_create_input("ch1", "twitter", "halo #UMKM", None, None)
    assert inp["mode"] == "shareNow"
    assert "dueAt" not in inp


# ---- verify_report ----

NOW = datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc)  # 07:00 WIB


def _post(service, due, status="scheduled"):
    return {"service": service, "status": status,
            "dueAt": due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "text": "x"}


def test_verify_lolos_dua_channel():
    posts = [_post("twitter", wib(2026, 7, 17, 7, 30)),
             _post("threads", wib(2026, 7, 17, 7, 30))]
    ok, lines = bp.verify_report(posts, "pagi", NOW)
    assert ok and all(line.startswith("OK") for line in lines)


def test_verify_gagal_satu_channel_kurang():
    ok, lines = bp.verify_report([_post("twitter", wib(2026, 7, 17, 7, 30))], "pagi", NOW)
    assert not ok
    assert any(line.startswith("MISS threads") for line in lines)


def test_verify_post_kadung_terbit_masih_dihitung():
    # verify jalan 09:00 WIB, post udah terbit 07:30 WIB (status sent) -> tetep OK
    posts = [_post("twitter", wib(2026, 7, 17, 7, 30), status="sent"),
             _post("threads", wib(2026, 7, 17, 7, 30), status="sent")]
    ok, _ = bp.verify_report(posts, "pagi", wib(2026, 7, 17, 9, 0))
    assert ok


def test_verify_tolak_slot_salah():
    # post jam sore nggak bikin slot pagi lolos
    posts = [_post("twitter", wib(2026, 7, 17, 19, 0)),
             _post("threads", wib(2026, 7, 17, 19, 0))]
    ok, _ = bp.verify_report(posts, "pagi", NOW)
    assert not ok


def test_verify_tolak_post_kemaren():
    # post pagi kemaren (>3 jam lookback) jangan bikin hari ini lolos
    posts = [_post("twitter", wib(2026, 7, 16, 7, 30), status="sent"),
             _post("threads", wib(2026, 7, 16, 7, 30), status="sent")]
    ok, _ = bp.verify_report(posts, "pagi", NOW)
    assert not ok


def test_verify_tolak_status_error_dan_draft():
    posts = [_post("twitter", wib(2026, 7, 17, 7, 30), status="error"),
             _post("threads", wib(2026, 7, 17, 7, 30), status="draft")]
    ok, _ = bp.verify_report(posts, "pagi", NOW)
    assert not ok


def test_verify_dueat_kosong_diabaikan():
    posts = [{"service": "twitter", "status": "scheduled", "dueAt": None, "text": "x"},
             _post("threads", wib(2026, 7, 17, 7, 30))]
    ok, lines = bp.verify_report(posts, "pagi", NOW)
    assert not ok
    assert any(line.startswith("MISS twitter") for line in lines)
