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


# ---- next_slot_due (jitter_minutes=0 -> deterministik, ngetes logika jam dasar) ----

def test_next_slot_pagi_masih_jauh():
    # 05:00 WIB -> slot pagi hari yang sama 07:30 WIB
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0), jitter_minutes=0)
    assert due.astimezone(WIB) == wib(2026, 7, 17, 7, 30)


def test_next_slot_kelewat_geser_besok():
    # 08:00 WIB (07:30 udah lewat) -> besok
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 8, 0), jitter_minutes=0)
    assert due.astimezone(WIB) == wib(2026, 7, 18, 7, 30)


def test_next_slot_mepet_kurang_dari_lead_geser_besok():
    # 07:25 WIB cuma 5 menit sebelum slot (< MIN_LEAD_MIN) -> besok
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 7, 25), jitter_minutes=0)
    assert due.astimezone(WIB) == wib(2026, 7, 18, 7, 30)


def test_next_slot_sore():
    due = bp.next_slot_due("sore", wib(2026, 7, 17, 10, 0), jitter_minutes=0)
    assert due.astimezone(WIB) == wib(2026, 7, 17, 19, 0)


def test_next_slot_hasilnya_utc():
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0), jitter_minutes=0)
    assert due.tzinfo == timezone.utc
    assert due.hour == 0 and due.minute == 30  # 07:30 WIB = 00:30 UTC


# ---- jitter (anti bot-signature: jangan post di detik sama persis tiap hari) ----

def test_next_slot_jitter_eksplisit_geser_sesuai_menit():
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0), jitter_minutes=10)
    assert due.astimezone(WIB) == wib(2026, 7, 17, 7, 40)
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0), jitter_minutes=-10)
    assert due.astimezone(WIB) == wib(2026, 7, 17, 7, 20)


def test_next_slot_jitter_default_random_dalam_batas_dan_bervariasi():
    seen = set()
    for _ in range(30):
        due = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0))
        delta_min = (due.astimezone(WIB) - wib(2026, 7, 17, 7, 30)).total_seconds() / 60
        assert -bp.JITTER_MINUTES <= delta_min <= bp.JITTER_MINUTES
        seen.add(delta_min)
    assert len(seen) > 1  # bukan nilai konstan tiap panggilan


def test_next_slot_jitter_tetep_dalam_window_verifikasi():
    # jitter maksimal (+/-15 menit) nggak boleh bikin due keluar dari in_slot_window
    due_plus = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0), jitter_minutes=bp.JITTER_MINUTES)
    due_minus = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0), jitter_minutes=-bp.JITTER_MINUTES)
    assert bp.in_slot_window("pagi", due_plus)
    assert bp.in_slot_window("pagi", due_minus)


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


# ---- promo_link / effective_length / with_promo_link (UTM tracking) ----

def test_promo_link_isi_utm_sesuai_service_dan_campaign():
    link = bp.promo_link("twitter", "pagi")
    assert "utm_source=twitter" in link
    assert "utm_campaign=pagi" in link
    assert link.startswith("https://marginin.com/")


def test_with_promo_link_nambahin_kalau_belum_ada():
    text = bp.with_promo_link("threads", "cerita jualan #UMKM", "sore")
    assert "utm_source=threads" in text
    assert text.startswith("cerita jualan #UMKM")


def test_with_promo_link_idempotent_kalau_udah_ada():
    text = "udah ada https://marginin.com/?utm_source=twitter&utm_medium=x&utm_campaign=y"
    assert bp.with_promo_link("twitter", text, "pagi") == text


def test_effective_length_twitter_shorten_url():
    long_url = "https://marginin.com/?utm_source=twitter&utm_medium=social&utm_campaign=promo_pagi"
    text = "halo #UMKM " + long_url
    eff = bp.effective_length("twitter", text)
    # panjang efektif = teks tanpa url + 23 char t.co, BUKAN panjang url asli
    assert eff == len("halo #UMKM " + "x" * 23)
    assert eff < len(text)


def test_effective_length_threads_apa_adanya():
    text = "halo " + "https://marginin.com/?utm_source=threads&utm_campaign=pagi"
    assert bp.effective_length("threads", text) == len(text)


def test_validate_text_pake_effective_length_bukan_raw():
    # teks + link panjang lolos di twitter karena link cuma dihitung 23 char
    link = bp.promo_link("twitter", "pagi")
    text = "#UMKM " + "x" * 240 + " " + link  # raw > 280, efektif <= 280
    assert len(text) > 280
    assert not bp.validate_text("twitter", text)


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


# ---- validate_thread / build_create_input (thread Twitter & Threads) ----

def test_validate_thread_twitter_hashtag_boleh_di_post_manapun():
    assert bp.validate_thread("twitter", ["tweet a", "tweet b", "tweet c"])  # tanpa hashtag -> error
    assert not bp.validate_thread("twitter", ["tweet a #UMKM", "tweet b", "tweet c"])
    assert not bp.validate_thread("twitter", ["tweet a", "tweet b", "tweet c #UMKM"])


def test_validate_thread_threads_nggak_wajib_hashtag():
    # threads: hashtag emang nggak dipake di teks (pakenya topic), jadi nggak wajib
    assert not bp.validate_thread("threads", ["post a", "post b", "post c"])


def test_validate_thread_tolak_post_kosong():
    errs = bp.validate_thread("twitter", ["tweet a #UMKM", "  ", "tweet c"])
    assert any("kosong" in e for e in errs)


def test_validate_thread_tolak_kepanjangan_sesuai_limit_service():
    errs_tw = bp.validate_thread("twitter", ["#UMKM " + "x" * 280, "tweet b"])
    assert any("maks" in e for e in errs_tw)
    errs_th = bp.validate_thread("threads", ["x" * 501, "post b"])
    assert any("maks" in e for e in errs_th)


def test_build_create_input_thread_masuk_metadata_twitter():
    inp = bp.build_create_input("ch1", "twitter", "tweet 1 #UMKM", "t", None,
                                thread=["tweet 2", "tweet 3"])
    thread = inp["metadata"]["twitter"]["thread"]
    assert [t["text"] for t in thread] == ["tweet 2", "tweet 3"]
    assert all(t["assets"] == [] for t in thread)


def test_build_create_input_thread_masuk_metadata_threads_bareng_topic():
    inp = bp.build_create_input("ch1", "threads", "post 1", "t", "umkmindonesia",
                                thread=["post 2", "post 3"])
    meta = inp["metadata"]["threads"]
    assert meta["topic"] == "umkmindonesia"
    assert [t["text"] for t in meta["thread"]] == ["post 2", "post 3"]


def test_build_create_input_tanpa_thread_nggak_ada_metadata_twitter():
    inp = bp.build_create_input("ch1", "twitter", "tweet 1 #UMKM", "t", None, thread=None)
    assert "metadata" not in inp


def test_build_create_input_threads_tanpa_thread_topic_tetep_jalan():
    inp = bp.build_create_input("ch1", "threads", "isi", "t", "umkmindonesia", thread=None)
    assert inp["metadata"] == {"threads": {"topic": "umkmindonesia"}}
