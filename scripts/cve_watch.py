#!/usr/bin/env python3
"""Watch security feeds for new CVE posts/articles, report them to the jetorbit Telegram.

Used two ways:
- deterministic loop verifier : `verify --source <src>`  (exit 0 = nothing left to do)
- agent tool via Bash         : `check --source <src>` (grounding), `notify` (report +
  mark it done)

Sources (SOURCES below) — each one has its own feed, its own "new" rule and its own
CVE-detection rule, but they share one state file (guid = article URL, so it's unique
across domains) and one Telegram destination (the jetorbit bot):

- cloudlinux: blog.cloudlinux.com/feed — <category>CVE</category> OR a CVE-ID in the
  title/description. "New" = pubDate < 24h old at run time (the blog posts rarely, so a
  rolling window is safe whatever hour we check).
- thn: feeds.feedburner.com/TheHackersNews — no <category>, so detection leans purely on
  a CVE-ID in the title+summary. "New" = pubDate FALLS ON THE SAME WIB DATE as the run
  ("published that day") — this feed is a firehose (dozens of articles/day, most of them
  NOT about CVEs), and a rolling 24h is a bad fit for "today" when the run doesn't happen
  at the same hour every day.

Scope note: CVE detection always needs a literal CVE-ID (CVE-YYYY-NNNNN) in the
title/summary/category — a zero-day without an official CVE number won't trip it (on
purpose, so we don't flood the report with every generic "critical flaw").

State (the guids ALREADY reported) is kept locally because there's no API to ask Telegram
"did I already send this message" — unlike buffer_post.py, which can cross-check against
Buffer. Pruned automatically (>30 days dropped) so it doesn't pile up forever.

Tokens in .env (TELEGRAM_BOT_TOKEN_JETORBIT / TELEGRAM_ALLOWED_CHAT_IDS_JETORBIT) —
NEVER put them in config.yaml.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import load_env  # noqa: E402

STATE_PATH = ROOT / "workspaces" / "jetorbit" / "state" / "cve_watch.json"
STATE_TTL_DAYS = 30
CVE_RE = re.compile(r"CVE-\d{4}-\d+", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
WIB = timezone(timedelta(hours=7))


def _fresh_rolling_24h(pub: datetime, now: datetime) -> bool:
    age = now - pub
    return timedelta(0) <= age <= timedelta(hours=24)


def _fresh_same_wib_day(pub: datetime, now: datetime) -> bool:
    if pub > now:
        return False
    return pub.astimezone(WIB).date() == now.astimezone(WIB).date()


SOURCES = {
    "cloudlinux": {
        "label": "the CloudLinux blog",
        "feed": "https://blog.cloudlinux.com/feed",
        "is_fresh": _fresh_rolling_24h,
    },
    "thn": {
        "label": "The Hacker News",
        "feed": "https://feeds.feedburner.com/TheHackersNews",
        "is_fresh": _fresh_same_wib_day,
    },
}


# ---- state (I/O — not tested directly, it just reads/writes JSON) ----

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ---- pure logic (tested without network — see tests/test_cve_watch.py) ----

def prune_state(state: dict, now: datetime) -> dict:
    cutoff = now - timedelta(days=STATE_TTL_DAYS)
    out = {}
    for guid, rec in state.items():
        try:
            ts = datetime.fromisoformat(rec["reported_at"])
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            out[guid] = rec
    return out


def is_cve_post(item: dict) -> bool:
    if any(c.strip().lower() == "cve" for c in item.get("categories", [])):
        return True
    return bool(CVE_RE.search(item["title"]) or CVE_RE.search(item["description"]))


def select_candidates(items: list[dict], state: dict, now: datetime, is_fresh) -> list[dict]:
    """CVE posts that are fresh by their source's rule and aren't in the state yet."""
    out = []
    for item in items:
        if not is_cve_post(item):
            continue
        if not is_fresh(item["pub"], now):
            continue
        if item["guid"] in state:
            continue
        out.append(item)
    return out


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        pub_raw = (item.findtext("pubDate") or "").strip()
        desc = (item.findtext("description") or "").strip()
        cats = [(c.text or "").strip() for c in item.findall("category")]
        try:
            pub = parsedate_to_datetime(pub_raw)
        except (TypeError, ValueError):
            continue
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        items.append({"guid": guid, "title": title, "link": link, "pub": pub,
                      "categories": cats, "description": TAG_RE.sub("", desc).strip()})
    return items


# ---- network ----

def fetch_feed(source: str) -> list[dict]:
    import httpx
    url = SOURCES[source]["feed"]
    r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0 (nloop cve-watch)"},
                 timeout=30, follow_redirects=True)
    r.raise_for_status()
    return parse_feed(r.text)


def _telegram_env() -> tuple[str, list[str]]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN_JETORBIT")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN_JETORBIT is not set (put it in the nloop .env)")
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS_JETORBIT", "")
    chat_ids = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()]
    if not chat_ids:
        sys.exit("TELEGRAM_ALLOWED_CHAT_IDS_JETORBIT is not set / is empty")
    return token, chat_ids


# ---- subcommands ----

def cmd_check(args) -> int:
    now = datetime.now(timezone.utc)
    src = SOURCES[args.source]
    cands = select_candidates(fetch_feed(args.source), load_state(), now, src["is_fresh"])
    if not cands:
        print(f"(no new CVE posts on {src['label']} left to report)")
        return 0
    for c in cands:
        age_h = (now - c["pub"]).total_seconds() / 3600
        print(f"guid: {c['guid']}")
        print(f"title: {c['title']}")
        print(f"link: {c['link']}")
        print(f"age: {age_h:.1f} hours ago ({c['pub'].isoformat()})")
        print(f"categories: {', '.join(c['categories'])}")
        print(f"feed summary: {c['description'][:500]}")
        print("---")
    return 0


def cmd_verify(args) -> int:
    now = datetime.now(timezone.utc)
    src = SOURCES[args.source]
    cands = select_candidates(fetch_feed(args.source), load_state(), now, src["is_fresh"])
    if not cands:
        print(f"OK — no new CVE posts on {src['label']} left to report")
        return 0
    print(f"NOT DONE: {len(cands)} new CVE post(s) on {src['label']} "
          f"not reported to Telegram yet:")
    for c in cands:
        print(f"- {c['title']} ({c['link']})")
    return 1


def cmd_notify(args) -> int:
    import httpx
    token, chat_ids = _telegram_env()
    for chat_id in chat_ids:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat_id, "text": args.text, "parse_mode": "HTML"},
                       timeout=30)
        if r.status_code != 200:
            print(f"failed to send to {chat_id}: {r.text}", file=sys.stderr)
            return 1
    now = datetime.now(timezone.utc)
    state = prune_state(load_state(), now)
    state[args.guid] = {"reported_at": now.isoformat(), "title": args.title or ""}
    save_state(state)
    print(f"OK reported & marked done: {args.guid}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_env(str(ROOT / ".env"))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="list new CVE posts that haven't been reported")
    c.add_argument("--source", choices=list(SOURCES), required=True)

    v = sub.add_parser("verify", help="loop verifier: have all new CVE posts been reported?")
    v.add_argument("--source", choices=list(SOURCES), required=True)

    n = sub.add_parser("notify",
                       help="send the report to the jetorbit Telegram + mark the guid done")
    n.add_argument("--guid", required=True)
    n.add_argument("--title", default=None)
    n.add_argument("--text", required=True,
                  help="message body (Telegram HTML: <b> <i> <a> <code>, NOT markdown)")

    args = ap.parse_args(argv)
    return {"check": cmd_check, "verify": cmd_verify, "notify": cmd_notify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
