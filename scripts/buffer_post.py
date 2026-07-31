#!/usr/bin/env python3
"""Buffer.com client (the new GraphQL API, api.buffer.com) for MarginIn's scheduled promos.

Used two ways:
- deterministic loop verifier : `verify --slot pagi|sore`  (exit 0 = post is scheduled)
- agent tool via Bash         : `post`, `recent`, `channels`

Content rules are ENFORCED here, not merely asked for in the prompt (so the agent
can't wriggle out of them):
- twitter: must have >=1 hashtag (wider UMKM reach), max 280 chars
- threads: must have a topic (default: umkmindonesia), max 500 chars
- the marginin.com CTA link is UTM-tagged & APPENDED AUTOMATICALLY by this script
  (the agent doesn't need to — and really shouldn't — type links by hand) so that
  traffic per channel/slot stays cleanly measurable in PostHog (see
  scripts/promo_report.py)

Primetime slots (WIB — the Buffer channels themselves are on Asia/Jakarta):
- pagi: posts 07:30, verification window 05:30-10:30
- sore: posts 19:00, verification window 17:00-22:00

Token lives in .env (BUFFER_ACCESS_TOKEN, gitignored) — NEVER put it in config.yaml.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import load_env  # noqa: E402

API = "https://api.buffer.com"
WIB = timezone(timedelta(hours=7))
SERVICES = ("twitter", "threads")
DEFAULT_TOPIC = "umkmindonesia"  # most common UMKM tag on Threads ID (alt: UMKMthreads)
LIMITS = {"twitter": 280, "threads": 500}

LINK_BASE = "https://marginin.com/"
URL_RE = re.compile(r"https?://\S+")
TWITTER_TCO_LEN = 23  # twitter shortens every URL to a 23-char t.co, real length irrelevant

# slot -> (post time "HH:MM" WIB, verification window (start, end) WIB)
SLOTS = {
    "pagi": ("07:30", ("05:30", "10:30")),
    "sore": ("19:00", ("17:00", "22:00")),
}
MIN_LEAD_MIN = 10          # dueAt must be at least 10 min out; if today's slot has
                           # already passed / is too close -> push to tomorrow
VERIFY_LOOKBACK_H = 3      # a post that went out WHILE the run was going still counts
VERIFY_LOOKAHEAD_H = 24
JITTER_MINUTES = 15        # random shift of the post time — without it posts go out at
                           # the EXACT SAME second every day (00:30:0X / 12:00:0X UTC in
                           # a row), a pattern X/Threads easily read as an automated
                           # account (bot signature) and quietly deprioritize the reach.


# ---------- pure helpers (tested in tests/test_buffer_post.py, no network) ----------

def _hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def next_slot_due(slot: str, now: datetime, jitter_minutes: int | None = None) -> datetime:
    """Next occurrence of the slot time (WIB) still >= MIN_LEAD_MIN ahead. Returns UTC.

    jitter_minutes: random shift of ±JITTER_MINUTES around the slot's base time
    (default: None -> random on every call). Pass 0 explicitly for deterministic tests."""
    h, m = _hhmm(SLOTS[slot][0])
    local = now.astimezone(WIB)
    due = local.replace(hour=h, minute=m, second=0, microsecond=0)
    jitter = random.randint(-JITTER_MINUTES, JITTER_MINUTES) if jitter_minutes is None else jitter_minutes
    due += timedelta(minutes=jitter)
    if due < local + timedelta(minutes=MIN_LEAD_MIN):
        due += timedelta(days=1)
    return due.astimezone(timezone.utc)


def in_slot_window(slot: str, due: datetime) -> bool:
    """Does the due time (WIB) land inside the slot's window?"""
    lo, hi = SLOTS[slot][1]
    t = due.astimezone(WIB)
    minutes = t.hour * 60 + t.minute
    lo_h, lo_m = _hhmm(lo)
    hi_h, hi_m = _hhmm(hi)
    return lo_h * 60 + lo_m <= minutes <= hi_h * 60 + hi_m


def promo_link(service: str, campaign: str) -> str:
    """marginin.com CTA link, UTM-tagged per channel/campaign — the basis of the
    attribution in scripts/promo_report.py. campaign is usually the slot name
    ("pagi"/"sore"), or "manual" for an off-schedule post --now. Deliberately no
    utm_medium (it's always "social", not worth tagging) to keep the link as short as
    possible — Threads counts its 500 chars verbatim (no shortening like twitter), so
    every char matters."""
    return f"{LINK_BASE}?utm_source={service}&utm_campaign={campaign}"


def effective_length(service: str, text: str) -> int:
    """Twitter always shortens URLs to t.co (23 chars) when counting the limit — the
    real link length is irrelevant. Threads doesn't shorten, it counts them as-is."""
    if service != "twitter":
        return len(text)
    return len(URL_RE.sub("x" * TWITTER_TCO_LEN, text))


def validate_text(service: str, text: str) -> list[str]:
    errs = []
    if not text.strip():
        errs.append("empty text")
    n = effective_length(service, text)
    if n > LIMITS[service]:
        errs.append(f"text is {n} chars (effective), max {service} {LIMITS[service]}")
    if service == "twitter" and "#" not in text:
        errs.append("a twitter post MUST have a hashtag (e.g. #UMKM #UMKMIndonesia)")
    return errs


def with_promo_link(service: str, text: str, campaign: str) -> str:
    """Append the UTM link to the end of the text, unless the agent already wrote a
    link itself (idempotent — checks for the 'utm_source=' substring)."""
    if "utm_source=" in text:
        return text
    return text.rstrip() + "\n\n" + promo_link(service, campaign)


def build_create_input(channel_id: str, service: str, text: str,
                       due_at: str | None, topic: str | None,
                       thread: list[str] | None = None) -> dict:
    """due_at None = publish RIGHT NOW (shareNow) — for manual off-slot posts.
    thread: follow-up posts (higher dwell-time/depth in the X & Threads algorithms
    than a single post) — both services support it, each wrapped differently."""
    inp = {
        "channelId": channel_id,
        "text": text,
        "assets": [],
        "schedulingType": "automatic",
        "mode": "customScheduled" if due_at else "shareNow",
    }
    if due_at:
        inp["dueAt"] = due_at
    thread_items = [{"text": t, "assets": []} for t in thread] if thread else None
    if service == "threads":
        meta = {"topic": topic or DEFAULT_TOPIC}
        if thread_items:
            meta["thread"] = thread_items
        inp["metadata"] = {"threads": meta}
    elif service == "twitter" and thread_items:
        inp["metadata"] = {"twitter": {"thread": thread_items}}
    return inp


def validate_thread(service: str, posts: list[str]) -> list[str]:
    """First post + follow-ups: the hashtag (twitter only, mandatory) may sit in any
    post (not required in every one — sticking it on all of them reads as spam), the
    length is still checked per-post against that service's limit."""
    errs = []
    if service == "twitter" and "#" not in " ".join(posts):
        errs.append("a thread MUST have a hashtag in one of its posts")
    for i, t in enumerate(posts, start=1):
        if not t.strip():
            errs.append(f"post #{i} is empty")
            continue
        n = effective_length(service, t)
        if n > LIMITS[service]:
            errs.append(f"post #{i}: {n} chars (effective), max {service} {LIMITS[service]}")
    return errs


def parse_due(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def verify_report(posts: list[dict], slot: str, now: datetime,
                  services: tuple[str, ...] = SERVICES) -> tuple[bool, list[str]]:
    """posts: [{service, status, dueAt, text}]. Every service needs >=1 scheduled/sent
    post whose dueAt is in [now-3h, now+24h] AND falls inside the slot window."""
    lo = now - timedelta(hours=VERIFY_LOOKBACK_H)
    hi = now + timedelta(hours=VERIFY_LOOKAHEAD_H)
    lines, ok = [], True
    for svc in services:
        hit = None
        for p in posts:
            if p["service"] != svc or p["status"] not in ("scheduled", "sending", "sent"):
                continue
            if not p.get("dueAt"):
                continue
            due = parse_due(p["dueAt"])
            if lo <= due <= hi and in_slot_window(slot, due):
                hit = due
                break
        if hit:
            lines.append(f"OK   {svc}: slot {slot} post scheduled for "
                         f"{hit.astimezone(WIB):%d %b %H:%M} WIB")
        else:
            ok = False
            lines.append(f"MISS {svc}: no post for slot {slot} yet "
                         f"(needs dueAt {SLOTS[slot][1][0]}-{SLOTS[slot][1][1]} WIB, "
                         f"within the next 24h)")
    return ok, lines


# ---------- Buffer GraphQL ----------

def gql(query: str, variables: dict | None = None) -> dict:
    import httpx
    token = os.environ.get("BUFFER_ACCESS_TOKEN")
    if not token:
        sys.exit("BUFFER_ACCESS_TOKEN is not set (put it in .env)")
    r = httpx.post(API, json={"query": query, "variables": variables or {}},
                   headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(f"Buffer API error: {json.dumps(data['errors'])}")
    return data["data"]


def get_org_id() -> str:
    return gql("{ account { organizations { id } } }")["account"]["organizations"][0]["id"]


def get_channels() -> list[dict]:
    q = """query($org: OrganizationId!) {
      channels(input: {organizationId: $org}) { id name service displayName timezone }
    }"""
    return gql(q, {"org": get_org_id()})["channels"]


def fetch_posts(limit: int = 50) -> list[dict]:
    """Latest posts across all channels, normalized to {service,status,dueAt,text}."""
    chans = {c["id"]: c["service"] for c in get_channels()}
    q = """query($org: OrganizationId!, $n: Int) {
      posts(input: {organizationId: $org}, first: $n) {
        edges { node { id text status dueAt channelId } }
      }
    }"""
    edges = gql(q, {"org": get_org_id(), "n": limit})["posts"]["edges"]
    return [{**e["node"], "service": chans.get(e["node"]["channelId"], "?")} for e in edges]


# ---------- subcommands ----------

def cmd_channels(_args) -> int:
    print(json.dumps(get_channels(), indent=2))
    return 0


def cmd_post(args) -> int:
    campaign = args.campaign or ("manual" if args.now else args.slot)
    thread_rest = None

    if args.thread:
        posts = [args.text, *args.thread]
        posts[-1] = with_promo_link(args.service, posts[-1], campaign)
        errs = validate_thread(args.service, posts)
        if errs:
            print("REJECTED:\n- " + "\n- ".join(errs), file=sys.stderr)
            return 1
        text, thread_rest = posts[0], posts[1:]
    else:
        text = with_promo_link(args.service, args.text, campaign)
        errs = validate_text(args.service, text)
        if errs:
            print("REJECTED:\n- " + "\n- ".join(errs), file=sys.stderr)
            return 1

    if args.now:
        due_iso = None
    else:
        due = (parse_due(args.at) if args.at
               else next_slot_due(args.slot, datetime.now(timezone.utc)))
        due_iso = due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    chan = next((c for c in get_channels() if c["service"] == args.service), None)
    if not chan:
        print(f"no {args.service} channel found in Buffer", file=sys.stderr)
        return 1
    inp = build_create_input(chan["id"], args.service, text, due_iso, args.topic, thread=thread_rest)
    if args.dry:
        print(json.dumps(inp, indent=2, ensure_ascii=False))
        return 0

    m = """mutation($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess { post { id status dueAt } }
        ... on MutationError { message }
      }
    }"""
    res = gql(m, {"input": inp})["createPost"]
    if "message" in res:
        print(f"Buffer rejected it: {res['message']}", file=sys.stderr)
        return 1
    p = res["post"]
    when = (f"{parse_due(p['dueAt']).astimezone(WIB):%d %b %H:%M} WIB"
            if p.get("dueAt") else "NOW")
    suffix = f" (thread, {1 + len(thread_rest)} tweets)" if thread_rest else ""
    print(f"OK {args.service} post {p['id']} -> {when}{suffix}")
    return 0


def cmd_recent(args) -> int:
    posts = fetch_posts(args.n * 2)
    posts.sort(key=lambda p: p.get("dueAt") or "", reverse=True)
    if not posts:
        print("(no posts yet)")
        return 0
    for p in posts[: args.n * 2]:
        due = f"{parse_due(p['dueAt']).astimezone(WIB):%d %b %H:%M}" if p.get("dueAt") else "-"
        text = " ".join((p.get("text") or "").split())
        print(f"[{p['service']}/{p['status']}] {due} WIB :: {text[:200]}")
    return 0


def cmd_relink(args) -> int:
    """Stick a UTM link onto a post that is ALREADY scheduled (created before this
    feature existed). Only safe for scheduled/draft — a sent post can't be edited."""
    chans = {c["id"]: c["service"] for c in get_channels()}
    posts = fetch_posts(50)
    target = next((p for p in posts if p["id"] == args.id), None)
    if not target:
        print(f"post {args.id} not found (run `recent` to get IDs)", file=sys.stderr)
        return 1
    if target["status"] not in ("scheduled", "draft"):
        print(f"post status '{target['status']}' — only scheduled/draft can be relinked",
              file=sys.stderr)
        return 1
    base_text = args.text or target["text"]
    text = with_promo_link(target["service"], base_text, args.campaign)
    if text == target["text"]:
        print("this post already has a UTM link, leaving it alone")
        return 0
    errs = validate_text(target["service"], text)
    if errs:
        print("REJECTED:\n- " + "\n- ".join(errs), file=sys.stderr)
        return 1
    inp = {
        "id": target["id"],
        "text": text,
        "schedulingType": "automatic",
        "mode": "customScheduled",
        "dueAt": target["dueAt"],
    }
    if target["service"] == "threads":
        inp["metadata"] = {"threads": {"topic": DEFAULT_TOPIC}}
    m = """mutation($input: EditPostInput!) {
      editPost(input: $input) {
        ... on PostActionSuccess { post { id status dueAt } }
        ... on MutationError { message }
      }
    }"""
    res = gql(m, {"input": inp})["editPost"]
    if "message" in res:
        print(f"Buffer rejected it: {res['message']}", file=sys.stderr)
        return 1
    print(f"OK relinked {target['service']} post {args.id}")
    return 0


def cmd_verify(args) -> int:
    ok, lines = verify_report(fetch_posts(), args.slot, datetime.now(timezone.utc))
    print("\n".join(lines))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    load_env(str(ROOT / ".env"))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("channels", help="list Buffer channels")

    p = sub.add_parser("post", help="create a scheduled post")
    p.add_argument("--service", choices=SERVICES, required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--topic", default=None, help=f"Threads topic (default: {DEFAULT_TOPIC})")
    p.add_argument("--slot", choices=list(SLOTS), default="pagi",
                   help="schedule into the next primetime slot (default: pagi)")
    p.add_argument("--at", default=None, help="override dueAt, ISO8601 UTC (rarely needed)")
    p.add_argument("--now", action="store_true", help="publish NOW (shareNow), skip the slot")
    p.add_argument("--campaign", default=None,
                   help="override utm_campaign (default: the slot name)")
    p.add_argument("--thread", action="append", default=None,
                   help="follow-up posts (twitter & threads) — repeat --thread per post, "
                        "in order. The CTA link goes on the LAST post, not the first.")
    p.add_argument("--dry", action="store_true", help="just print the payload, don't post")

    r = sub.add_parser("recent", help="latest posts (anti-repeat / inspiration)")
    r.add_argument("-n", type=int, default=10)

    v = sub.add_parser("verify", help="loop verifier: is the slot scheduled on both channels?")
    v.add_argument("--slot", choices=list(SLOTS), required=True)

    rl = sub.add_parser("relink", help="add a UTM link to a scheduled post that lacks one")
    rl.add_argument("--id", required=True)
    rl.add_argument("--campaign", required=True)
    rl.add_argument("--text", default=None,
                    help="replace the base text before the link is added (for old posts "
                         "that are too long)")

    args = ap.parse_args(argv)
    return {"channels": cmd_channels, "post": cmd_post, "relink": cmd_relink,
            "recent": cmd_recent, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
