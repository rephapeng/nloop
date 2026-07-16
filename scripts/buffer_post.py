#!/usr/bin/env python3
"""Klien Buffer.com (GraphQL baru, api.buffer.com) buat promo terjadwal MarginIn.

Dipake dua arah:
- verifier deterministik loop : `verify --slot pagi|sore`  (exit 0 = post kejadwal)
- tool si agent via Bash      : `post`, `recent`, `channels`

Aturan konten DIPAKSA di sini, bukan cuma diminta di prompt (agent nggak bisa lolos):
- twitter: wajib >=1 hashtag (jangkauan UMKM lebih luas), maks 280 char
- threads: wajib topic (default: umkmindonesia), maks 500 char

Slot primetime (WIB — channel Buffer-nya timezone Asia/Jakarta):
- pagi: post 07:30, window verifikasi 05:30-10:30
- sore: post 19:00, window verifikasi 17:00-22:00

Token di .env (BUFFER_ACCESS_TOKEN, gitignored) — JANGAN pernah ke config.yaml.
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

API = "https://api.buffer.com"
WIB = timezone(timedelta(hours=7))
SERVICES = ("twitter", "threads")
DEFAULT_TOPIC = "umkmindonesia"  # tag UMKM paling umum di Threads ID (alternatif: UMKMthreads)
LIMITS = {"twitter": 280, "threads": 500}

# slot -> (jam post "HH:MM" WIB, window verifikasi (mulai, selesai) WIB)
SLOTS = {
    "pagi": ("07:30", ("05:30", "10:30")),
    "sore": ("19:00", ("17:00", "22:00")),
}
MIN_LEAD_MIN = 10          # dueAt minimal 10 menit di depan; kalau slot hari ini
                           # udah lewat/mepet -> geser ke besok
VERIFY_LOOKBACK_H = 3      # post yang KEBURU terbit saat run masih jalan tetep dihitung
VERIFY_LOOKAHEAD_H = 24


# ---------- pure helpers (dites di tests/test_buffer_post.py, tanpa network) ----------

def _hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def next_slot_due(slot: str, now: datetime) -> datetime:
    """Kemunculan jam slot (WIB) berikutnya yang masih >= MIN_LEAD_MIN di depan. UTC."""
    h, m = _hhmm(SLOTS[slot][0])
    local = now.astimezone(WIB)
    due = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if due < local + timedelta(minutes=MIN_LEAD_MIN):
        due += timedelta(days=1)
    return due.astimezone(timezone.utc)


def in_slot_window(slot: str, due: datetime) -> bool:
    """Jam-nya due (WIB) jatuh di window slot?"""
    lo, hi = SLOTS[slot][1]
    t = due.astimezone(WIB)
    minutes = t.hour * 60 + t.minute
    lo_h, lo_m = _hhmm(lo)
    hi_h, hi_m = _hhmm(hi)
    return lo_h * 60 + lo_m <= minutes <= hi_h * 60 + hi_m


def validate_text(service: str, text: str) -> list[str]:
    errs = []
    if not text.strip():
        errs.append("teks kosong")
    if len(text) > LIMITS[service]:
        errs.append(f"teks {len(text)} char, maks {service} {LIMITS[service]}")
    if service == "twitter" and "#" not in text:
        errs.append("post twitter WAJIB ada hashtag (mis. #UMKM #UMKMIndonesia)")
    return errs


def build_create_input(channel_id: str, service: str, text: str,
                       due_at: str | None, topic: str | None) -> dict:
    """due_at None = terbit SEKARANG (shareNow) — buat post manual di luar slot."""
    inp = {
        "channelId": channel_id,
        "text": text,
        "assets": [],
        "schedulingType": "automatic",
        "mode": "customScheduled" if due_at else "shareNow",
    }
    if due_at:
        inp["dueAt"] = due_at
    if service == "threads":
        inp["metadata"] = {"threads": {"topic": topic or DEFAULT_TOPIC}}
    return inp


def parse_due(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def verify_report(posts: list[dict], slot: str, now: datetime,
                  services: tuple[str, ...] = SERVICES) -> tuple[bool, list[str]]:
    """posts: [{service, status, dueAt, text}]. Tiap service wajib punya >=1 post
    scheduled/sent yang dueAt-nya di [now-3h, now+24h] DAN jatuh di window slot."""
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
            lines.append(f"OK   {svc}: post slot {slot} kejadwal {hit.astimezone(WIB):%d %b %H:%M} WIB")
        else:
            ok = False
            lines.append(f"MISS {svc}: belum ada post slot {slot} "
                         f"(butuh dueAt {SLOTS[slot][1][0]}-{SLOTS[slot][1][1]} WIB, <24 jam ke depan)")
    return ok, lines


# ---------- Buffer GraphQL ----------

def gql(query: str, variables: dict | None = None) -> dict:
    import httpx
    token = os.environ.get("BUFFER_ACCESS_TOKEN")
    if not token:
        sys.exit("BUFFER_ACCESS_TOKEN belum diset (isi di .env)")
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
    """Post terbaru semua channel, dinormalisasi ke {service,status,dueAt,text}."""
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
    errs = validate_text(args.service, args.text)
    if errs:
        print("DITOLAK:\n- " + "\n- ".join(errs), file=sys.stderr)
        return 1
    if args.now:
        due_iso = None
    else:
        due = (parse_due(args.at) if args.at
               else next_slot_due(args.slot, datetime.now(timezone.utc)))
        due_iso = due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    chan = next((c for c in get_channels() if c["service"] == args.service), None)
    if not chan:
        print(f"channel {args.service} nggak ketemu di Buffer", file=sys.stderr)
        return 1
    inp = build_create_input(chan["id"], args.service, args.text, due_iso, args.topic)
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
        print(f"Buffer nolak: {res['message']}", file=sys.stderr)
        return 1
    p = res["post"]
    when = (f"{parse_due(p['dueAt']).astimezone(WIB):%d %b %H:%M} WIB"
            if p.get("dueAt") else "SEKARANG")
    print(f"OK {args.service} post {p['id']} -> {when}")
    return 0


def cmd_recent(args) -> int:
    posts = fetch_posts(args.n * 2)
    posts.sort(key=lambda p: p.get("dueAt") or "", reverse=True)
    if not posts:
        print("(belum ada post)")
        return 0
    for p in posts[: args.n * 2]:
        due = f"{parse_due(p['dueAt']).astimezone(WIB):%d %b %H:%M}" if p.get("dueAt") else "-"
        text = " ".join((p.get("text") or "").split())
        print(f"[{p['service']}/{p['status']}] {due} WIB :: {text[:200]}")
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

    sub.add_parser("channels", help="list channel Buffer")

    p = sub.add_parser("post", help="bikin post terjadwal")
    p.add_argument("--service", choices=SERVICES, required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--topic", default=None, help=f"topic Threads (default: {DEFAULT_TOPIC})")
    p.add_argument("--slot", choices=list(SLOTS), default="pagi",
                   help="jadwal ke slot primetime berikutnya (default: pagi)")
    p.add_argument("--at", default=None, help="override dueAt ISO8601 UTC (jarang perlu)")
    p.add_argument("--now", action="store_true", help="terbit SEKARANG (shareNow), skip slot")
    p.add_argument("--dry", action="store_true", help="print payload doang, nggak ngepost")

    r = sub.add_parser("recent", help="post terakhir (anti-repeat / inspirasi)")
    r.add_argument("-n", type=int, default=10)

    v = sub.add_parser("verify", help="verifier loop: slot kejadwal di kedua channel?")
    v.add_argument("--slot", choices=list(SLOTS), required=True)

    args = ap.parse_args(argv)
    return {"channels": cmd_channels, "post": cmd_post,
            "recent": cmd_recent, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
