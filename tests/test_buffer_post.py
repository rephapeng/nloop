"""scripts/buffer_post.py: pure helpers + verify_report — no network.

The script isn't a package, so it's imported via importlib. The functions that touch
the Buffer API (gql/fetch_posts) aren't tested here — what is tested is the WIB slot
logic, content validation (twitter needs a hashtag, threads needs a topic), and the
verifier.
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


# ---- next_slot_due (jitter_minutes=0 -> deterministic, tests the base-time logic) ----

def test_next_slot_pagi_still_ahead():
    # 05:00 WIB -> slot pagi the same day at 07:30 WIB
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0), jitter_minutes=0)
    assert due.astimezone(WIB) == wib(2026, 7, 17, 7, 30)


def test_next_slot_missed_moves_to_tomorrow():
    # 08:00 WIB (07:30 already gone) -> tomorrow
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 8, 0), jitter_minutes=0)
    assert due.astimezone(WIB) == wib(2026, 7, 18, 7, 30)


def test_next_slot_too_close_moves_to_tomorrow():
    # 07:25 WIB is only 5 min before the slot (< MIN_LEAD_MIN) -> tomorrow
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 7, 25), jitter_minutes=0)
    assert due.astimezone(WIB) == wib(2026, 7, 18, 7, 30)


def test_next_slot_sore():
    due = bp.next_slot_due("sore", wib(2026, 7, 17, 10, 0), jitter_minutes=0)
    assert due.astimezone(WIB) == wib(2026, 7, 17, 19, 0)


def test_next_slot_returns_utc():
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0), jitter_minutes=0)
    assert due.tzinfo == timezone.utc
    assert due.hour == 0 and due.minute == 30  # 07:30 WIB = 00:30 UTC


# ---- jitter (anti bot-signature: never post at the exact same second every day) ----

def test_next_slot_explicit_jitter_shifts_by_minutes():
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0), jitter_minutes=10)
    assert due.astimezone(WIB) == wib(2026, 7, 17, 7, 40)
    due = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0), jitter_minutes=-10)
    assert due.astimezone(WIB) == wib(2026, 7, 17, 7, 20)


def test_next_slot_default_jitter_random_within_bounds_and_varies():
    seen = set()
    for _ in range(30):
        due = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0))
        delta_min = (due.astimezone(WIB) - wib(2026, 7, 17, 7, 30)).total_seconds() / 60
        assert -bp.JITTER_MINUTES <= delta_min <= bp.JITTER_MINUTES
        seen.add(delta_min)
    assert len(seen) > 1  # not a constant on every call


def test_next_slot_jitter_stays_inside_verify_window():
    # max jitter (+/-15 min) must never push due outside in_slot_window
    due_plus = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0), jitter_minutes=bp.JITTER_MINUTES)
    due_minus = bp.next_slot_due("pagi", wib(2026, 7, 17, 5, 0), jitter_minutes=-bp.JITTER_MINUTES)
    assert bp.in_slot_window("pagi", due_plus)
    assert bp.in_slot_window("pagi", due_minus)


# ---- in_slot_window ----

def test_window_pagi():
    assert bp.in_slot_window("pagi", wib(2026, 7, 17, 7, 30))
    assert bp.in_slot_window("pagi", wib(2026, 7, 17, 5, 30))   # lower bound inclusive
    assert bp.in_slot_window("pagi", wib(2026, 7, 17, 10, 30))  # upper bound inclusive
    assert not bp.in_slot_window("pagi", wib(2026, 7, 17, 11, 0))
    assert not bp.in_slot_window("pagi", wib(2026, 7, 17, 19, 0))  # evening hour


def test_window_uses_wib_hour_not_utc():
    # 00:30 UTC = 07:30 WIB -> inside the pagi window even though UTC says pre-dawn
    assert bp.in_slot_window("pagi", datetime(2026, 7, 17, 0, 30, tzinfo=timezone.utc))


# ---- validate_text ----

def test_twitter_requires_hashtag():
    assert bp.validate_text("twitter", "promo without a hashtag")
    assert not bp.validate_text("twitter", "promo #UMKM")


def test_length_limits():
    assert bp.validate_text("twitter", "#UMKM " + "x" * 280)
    assert not bp.validate_text("twitter", "#UMKM " + "x" * 270)
    assert bp.validate_text("threads", "x" * 501)
    assert not bp.validate_text("threads", "x" * 500)


def test_empty_text_rejected():
    assert bp.validate_text("twitter", "   ")


# ---- build_create_input ----

def test_threads_gets_default_topic():
    inp = bp.build_create_input("ch1", "threads", "hello", "2026-07-17T00:30:00.000Z", None)
    assert inp["metadata"]["threads"]["topic"] == bp.DEFAULT_TOPIC
    assert inp["mode"] == "customScheduled"
    assert inp["schedulingType"] == "automatic"
    assert inp["assets"] == []


def test_threads_custom_topic():
    inp = bp.build_create_input("ch1", "threads", "hello", "t", "UMKMthreads")
    assert inp["metadata"]["threads"]["topic"] == "UMKMthreads"


def test_twitter_has_no_metadata():
    inp = bp.build_create_input("ch1", "twitter", "hello #UMKM", "t", None)
    assert "metadata" not in inp


def test_due_none_becomes_share_now():
    inp = bp.build_create_input("ch1", "twitter", "hello #UMKM", None, None)
    assert inp["mode"] == "shareNow"
    assert "dueAt" not in inp


# ---- promo_link / effective_length / with_promo_link (UTM tracking) ----

def test_promo_link_utm_matches_service_and_campaign():
    link = bp.promo_link("twitter", "pagi")
    assert "utm_source=twitter" in link
    assert "utm_campaign=pagi" in link
    assert link.startswith("https://marginin.com/")


def test_with_promo_link_appends_when_missing():
    text = bp.with_promo_link("threads", "selling story #UMKM", "sore")
    assert "utm_source=threads" in text
    assert text.startswith("selling story #UMKM")


def test_with_promo_link_idempotent_when_already_there():
    text = "already there https://marginin.com/?utm_source=twitter&utm_medium=x&utm_campaign=y"
    assert bp.with_promo_link("twitter", text, "pagi") == text


def test_effective_length_twitter_shortens_url():
    long_url = "https://marginin.com/?utm_source=twitter&utm_medium=social&utm_campaign=promo_pagi"
    text = "hello #UMKM " + long_url
    eff = bp.effective_length("twitter", text)
    # effective length = text minus the url + 23 t.co chars, NOT the real url length
    assert eff == len("hello #UMKM " + "x" * 23)
    assert eff < len(text)


def test_effective_length_threads_counts_as_is():
    text = "hello " + "https://marginin.com/?utm_source=threads&utm_campaign=pagi"
    assert bp.effective_length("threads", text) == len(text)


def test_validate_text_uses_effective_length_not_raw():
    # text + a long link passes on twitter because the link only counts as 23 chars
    link = bp.promo_link("twitter", "pagi")
    text = "#UMKM " + "x" * 240 + " " + link  # raw > 280, effective <= 280
    assert len(text) > 280
    assert not bp.validate_text("twitter", text)


# ---- verify_report ----

NOW = datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc)  # 07:00 WIB


def _post(service, due, status="scheduled"):
    return {"service": service, "status": status,
            "dueAt": due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "text": "x"}


def test_verify_passes_on_both_channels():
    posts = [_post("twitter", wib(2026, 7, 17, 7, 30)),
             _post("threads", wib(2026, 7, 17, 7, 30))]
    ok, lines = bp.verify_report(posts, "pagi", NOW)
    assert ok and all(line.startswith("OK") for line in lines)


def test_verify_fails_when_one_channel_is_missing():
    ok, lines = bp.verify_report([_post("twitter", wib(2026, 7, 17, 7, 30))], "pagi", NOW)
    assert not ok
    assert any(line.startswith("MISS threads") for line in lines)


def test_verify_post_already_published_still_counts():
    # verify runs 09:00 WIB, the post already went out 07:30 WIB (status sent) -> still OK
    posts = [_post("twitter", wib(2026, 7, 17, 7, 30), status="sent"),
             _post("threads", wib(2026, 7, 17, 7, 30), status="sent")]
    ok, _ = bp.verify_report(posts, "pagi", wib(2026, 7, 17, 9, 0))
    assert ok


def test_verify_rejects_wrong_slot():
    # an evening post must not make slot pagi pass
    posts = [_post("twitter", wib(2026, 7, 17, 19, 0)),
             _post("threads", wib(2026, 7, 17, 19, 0))]
    ok, _ = bp.verify_report(posts, "pagi", NOW)
    assert not ok


def test_verify_rejects_yesterdays_post():
    # yesterday's morning post (>3h lookback) must not make today pass
    posts = [_post("twitter", wib(2026, 7, 16, 7, 30), status="sent"),
             _post("threads", wib(2026, 7, 16, 7, 30), status="sent")]
    ok, _ = bp.verify_report(posts, "pagi", NOW)
    assert not ok


def test_verify_rejects_error_and_draft_status():
    posts = [_post("twitter", wib(2026, 7, 17, 7, 30), status="error"),
             _post("threads", wib(2026, 7, 17, 7, 30), status="draft")]
    ok, _ = bp.verify_report(posts, "pagi", NOW)
    assert not ok


def test_verify_ignores_empty_dueat():
    posts = [{"service": "twitter", "status": "scheduled", "dueAt": None, "text": "x"},
             _post("threads", wib(2026, 7, 17, 7, 30))]
    ok, lines = bp.verify_report(posts, "pagi", NOW)
    assert not ok
    assert any(line.startswith("MISS twitter") for line in lines)


# ---- validate_thread / build_create_input (Twitter & Threads threads) ----

def test_validate_thread_twitter_hashtag_allowed_in_any_post():
    assert bp.validate_thread("twitter", ["tweet a", "tweet b", "tweet c"])  # no hashtag -> error
    assert not bp.validate_thread("twitter", ["tweet a #UMKM", "tweet b", "tweet c"])
    assert not bp.validate_thread("twitter", ["tweet a", "tweet b", "tweet c #UMKM"])


def test_validate_thread_threads_does_not_require_hashtag():
    # threads: hashtags aren't used in the text at all (topic does the job), so not required
    assert not bp.validate_thread("threads", ["post a", "post b", "post c"])


def test_validate_thread_rejects_empty_post():
    errs = bp.validate_thread("twitter", ["tweet a #UMKM", "  ", "tweet c"])
    assert any("empty" in e for e in errs)


def test_validate_thread_rejects_too_long_per_service_limit():
    errs_tw = bp.validate_thread("twitter", ["#UMKM " + "x" * 280, "tweet b"])
    assert any("max" in e for e in errs_tw)
    errs_th = bp.validate_thread("threads", ["x" * 501, "post b"])
    assert any("max" in e for e in errs_th)


def test_build_create_input_thread_goes_into_twitter_metadata():
    inp = bp.build_create_input("ch1", "twitter", "tweet 1 #UMKM", "t", None,
                                thread=["tweet 2", "tweet 3"])
    thread = inp["metadata"]["twitter"]["thread"]
    assert [t["text"] for t in thread] == ["tweet 2", "tweet 3"]
    assert all(t["assets"] == [] for t in thread)


def test_build_create_input_thread_goes_into_threads_metadata_with_topic():
    inp = bp.build_create_input("ch1", "threads", "post 1", "t", "umkmindonesia",
                                thread=["post 2", "post 3"])
    meta = inp["metadata"]["threads"]
    assert meta["topic"] == "umkmindonesia"
    assert [t["text"] for t in meta["thread"]] == ["post 2", "post 3"]


def test_build_create_input_without_thread_has_no_twitter_metadata():
    inp = bp.build_create_input("ch1", "twitter", "tweet 1 #UMKM", "t", None, thread=None)
    assert "metadata" not in inp


def test_build_create_input_threads_without_thread_still_keeps_topic():
    inp = bp.build_create_input("ch1", "threads", "content", "t", "umkmindonesia", thread=None)
    assert inp["metadata"] == {"threads": {"topic": "umkmindonesia"}}
