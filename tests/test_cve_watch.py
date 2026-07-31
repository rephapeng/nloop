"""scripts/cve_watch.py: feed parsing + candidate filtering — no network.

The script isn't a package, so it's imported via importlib (same pattern as
test_buffer_post.py). fetch_feed/notify (network/Telegram) aren't tested here.
"""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "cve_watch", Path(__file__).parent.parent / "scripts" / "cve_watch.py")
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def item(guid="g1", title="Some post", pub=None, categories=None, hours_ago=1):
    return {
        "guid": guid,
        "title": title,
        "link": f"https://blog.cloudlinux.com/{guid}/",
        "pub": pub or (NOW - timedelta(hours=hours_ago)),
        "categories": categories or [],
        "description": "desc",
    }


FEED_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item>
  <title>OVSwrap (CVE-2026-64531) local root exploit</title>
  <link>https://blog.cloudlinux.com/ovswrap-cve-2026-64531-mitigation/</link>
  <guid isPermaLink="false">https://blog.cloudlinux.com/?p=266001</guid>
  <pubDate>Fri, 31 Jul 2026 11:00:00 +0000</pubDate>
  <category>CloudLinux</category>
  <category>CVE</category>
  <description>&lt;p&gt;A kernel flaw giving local root.&lt;/p&gt;</description>
</item>
<item>
  <title>CloudLinux 10 release notes</title>
  <link>https://blog.cloudlinux.com/cl10-notes/</link>
  <guid isPermaLink="false">https://blog.cloudlinux.com/?p=266002</guid>
  <pubDate>Fri, 31 Jul 2026 09:00:00 +0000</pubDate>
  <category>CloudLinux</category>
  <description>&lt;p&gt;What's new.&lt;/p&gt;</description>
</item>
</channel></rss>"""


# ---- parse_feed ----

def test_parse_feed_extracts_fields():
    items = cw.parse_feed(FEED_XML)
    assert len(items) == 2
    cve_item = items[0]
    assert cve_item["guid"] == "https://blog.cloudlinux.com/?p=266001"
    assert cve_item["categories"] == ["CloudLinux", "CVE"]
    assert cve_item["pub"] == datetime(2026, 7, 31, 11, 0, tzinfo=timezone.utc)
    assert cve_item["description"] == "A kernel flaw giving local root."


# ---- is_cve_post ----

def test_is_cve_post_by_category():
    assert cw.is_cve_post(item(title="release notes", categories=["CVE"]))


def test_is_cve_post_by_title():
    assert cw.is_cve_post(item(title="OVSwrap (CVE-2026-64531) mitigation", categories=[]))


def test_is_cve_post_neither():
    assert not cw.is_cve_post(item(title="CloudLinux 10 release notes", categories=["CloudLinux"]))


def test_is_cve_post_case_insensitive_title():
    assert cw.is_cve_post(item(title="cve-2026-1 found", categories=[]))


# ---- select_candidates (uses cloudlinux's rolling-24h freshness rule) ----

FRESH_24H = cw.SOURCES["cloudlinux"]["is_fresh"]


def test_select_candidates_within_24h():
    items = [item(guid="a", categories=["CVE"], hours_ago=1)]
    out = cw.select_candidates(items, {}, NOW, FRESH_24H)
    assert [i["guid"] for i in out] == ["a"]


def test_select_candidates_excludes_older_than_24h():
    items = [item(guid="a", categories=["CVE"], hours_ago=25)]
    assert cw.select_candidates(items, {}, NOW, FRESH_24H) == []


def test_select_candidates_excludes_non_cve():
    items = [item(guid="a", categories=["CloudLinux"], hours_ago=1)]
    assert cw.select_candidates(items, {}, NOW, FRESH_24H) == []


def test_select_candidates_excludes_already_reported():
    items = [item(guid="a", categories=["CVE"], hours_ago=1)]
    state = {"a": {"reported_at": NOW.isoformat()}}
    assert cw.select_candidates(items, state, NOW, FRESH_24H) == []


def test_select_candidates_excludes_future_pubdate():
    # feed clock skew / bug -> never treat a post with a future pubDate as "new"
    items = [item(guid="a", categories=["CVE"], hours_ago=-1)]
    assert cw.select_candidates(items, {}, NOW, FRESH_24H) == []


# ---- thn freshness: "published that day" = same WIB date, not a rolling 24h ----

FRESH_THN = cw.SOURCES["thn"]["is_fresh"]


def test_thn_fresh_same_wib_day():
    # NOW = 2026-08-01 12:00 UTC = 2026-08-01 19:00 WIB
    pub_earlier_today_wib = NOW.replace(hour=1)  # 2026-08-01 01:00 UTC = 08:00 WIB, same day
    assert FRESH_THN(pub_earlier_today_wib, NOW)


def test_thn_not_fresh_previous_wib_day():
    # NOW WIB = 2026-08-01 19:00. A pub at 16:00 UTC the day before = 2026-07-31
    # 23:00 WIB -> still the 31st, a different WIB day from NOW
    pub_yesterday = (NOW - timedelta(days=1)).replace(hour=16)
    assert not FRESH_THN(pub_yesterday, NOW)


def test_thn_not_fresh_future_pubdate():
    assert not FRESH_THN(NOW + timedelta(hours=1), NOW)


def test_select_candidates_detects_cve_in_description_not_title():
    # THN feed: the title often doesn't name a CVE-ID but the summary does
    it = item(guid="a", title="Critical Flaw Lets Attackers Run Code", hours_ago=1)
    it["description"] = "Tracked as CVE-2026-12345, the bug allows..."
    assert cw.select_candidates([it], {}, NOW, FRESH_24H) == [it]


# ---- prune_state ----

def test_prune_state_drops_old_entries():
    state = {
        "old": {"reported_at": (NOW - timedelta(days=31)).isoformat()},
        "recent": {"reported_at": (NOW - timedelta(days=1)).isoformat()},
    }
    out = cw.prune_state(state, NOW)
    assert list(out) == ["recent"]


def test_prune_state_drops_malformed_entries():
    state = {"bad": {}}
    assert cw.prune_state(state, NOW) == {}
