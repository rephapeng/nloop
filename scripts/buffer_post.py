#!/usr/bin/env python3
"""Klien Buffer.com (GraphQL baru, api.buffer.com) buat promo terjadwal MarginIn.

Dipake dua arah:
- verifier deterministik loop : `verify --slot pagi|sore`  (exit 0 = post kejadwal)
- tool si agent via Bash      : `post`, `recent`, `channels`

Aturan konten DIPAKSA di sini, bukan cuma diminta di prompt (agent nggak bisa lolos):
- twitter: wajib >=1 hashtag (jangkauan UMKM lebih luas), maks 280 char
- threads: wajib topic (default: umkmindonesia), maks 500 char
- link CTA marginin.com di-UTM-tag & DI-APPEND OTOMATIS oleh script (agent nggak
  perlu, dan sebaiknya nggak, ngetik link manual) — biar traffic per channel/slot
  keukur bersih di PostHog (lihat scripts/promo_report.py)

Slot primetime (WIB — channel Buffer-nya timezone Asia/Jakarta):
- pagi: post 07:30, window verifikasi 05:30-10:30
- sore: post 19:00, window verifikasi 17:00-22:00

Token di .env (BUFFER_ACCESS_TOKEN, gitignored) — JANGAN pernah ke config.yaml.
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
DEFAULT_TOPIC = "umkmindonesia"  # tag UMKM paling umum di Threads ID (alternatif: UMKMthreads)
LIMITS = {"twitter": 280, "threads": 500}

LINK_BASE = "https://marginin.com/"
URL_RE = re.compile(r"https?://\S+")
TWITTER_TCO_LEN = 23  # twitter selalu nge-shorten URL ke t.co 23 char, panjang asli nggak ngaruh ke limit

# slot -> (jam post "HH:MM" WIB, window verifikasi (mulai, selesai) WIB)
SLOTS = {
    "pagi": ("07:30", ("05:30", "10:30")),
    "sore": ("19:00", ("17:00", "22:00")),
}
MIN_LEAD_MIN = 10          # dueAt minimal 10 menit di depan; kalau slot hari ini
                           # udah lewat/mepet -> geser ke besok
VERIFY_LOOKBACK_H = 3      # post yang KEBURU terbit saat run masih jalan tetep dihitung
VERIFY_LOOKAHEAD_H = 24
JITTER_MINUTES = 15        # variasi acak jam post — tanpa ini, post keluar di detik
                           # yang SAMA PERSIS tiap hari (00:30:0X / 12:00:0X UTC beruntun),
                           # pola gampang kebaca X/Threads sebagai akun otomatis (bot
                           # signature) dan bikin reach di-deprioritize diam-diam.


# ---------- pure helpers (dites di tests/test_buffer_post.py, tanpa network) ----------

def _hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def next_slot_due(slot: str, now: datetime, jitter_minutes: int | None = None) -> datetime:
    """Kemunculan jam slot (WIB) berikutnya yang masih >= MIN_LEAD_MIN di depan. UTC.

    jitter_minutes: geser acak ±JITTER_MINUTES dari jam dasar slot (default:
    None -> random tiap panggilan). Kasih 0 eksplisit buat testing deterministik."""
    h, m = _hhmm(SLOTS[slot][0])
    local = now.astimezone(WIB)
    due = local.replace(hour=h, minute=m, second=0, microsecond=0)
    jitter = random.randint(-JITTER_MINUTES, JITTER_MINUTES) if jitter_minutes is None else jitter_minutes
    due += timedelta(minutes=jitter)
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


def promo_link(service: str, campaign: str) -> str:
    """Link CTA marginin.com di-tag UTM per channel/campaign — dasar attribution
    di scripts/promo_report.py. campaign biasanya nama slot ("pagi"/"sore"), atau
    "manual" buat post --now di luar jadwal. Sengaja tanpa utm_medium (selalu
    "social", nggak perlu ditag) biar link sependek mungkin — Threads limitnya
    500 char apa adanya (nggak di-shorten kaya twitter), jadi tiap char berharga."""
    return f"{LINK_BASE}?utm_source={service}&utm_campaign={campaign}"


def effective_length(service: str, text: str) -> int:
    """Twitter selalu nge-shorten URL jadi t.co (23 char) buat hitung limit —
    panjang asli link nggak ngaruh. Threads nggak nge-shorten, dihitung apa adanya."""
    if service != "twitter":
        return len(text)
    return len(URL_RE.sub("x" * TWITTER_TCO_LEN, text))


def validate_text(service: str, text: str) -> list[str]:
    errs = []
    if not text.strip():
        errs.append("teks kosong")
    n = effective_length(service, text)
    if n > LIMITS[service]:
        errs.append(f"teks {n} char (efektif), maks {service} {LIMITS[service]}")
    if service == "twitter" and "#" not in text:
        errs.append("post twitter WAJIB ada hashtag (mis. #UMKM #UMKMIndonesia)")
    return errs


def with_promo_link(service: str, text: str, campaign: str) -> str:
    """Append link UTM ke akhir teks, kecuali agent udah nulis link sendiri
    (idempotent — cek substring 'utm_source=')."""
    if "utm_source=" in text:
        return text
    return text.rstrip() + "\n\n" + promo_link(service, campaign)


def build_create_input(channel_id: str, service: str, text: str,
                       due_at: str | None, topic: str | None,
                       thread: list[str] | None = None) -> dict:
    """due_at None = terbit SEKARANG (shareNow) — buat post manual di luar slot.
    thread: post lanjutan (dwell-time/depth lebih tinggi di algoritma X & Threads
    dibanding single post) — didukung dua-duanya, dibungkus beda per-service."""
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
    """Post pertama + lanjutan: hashtag (twitter doang, wajib) boleh di post
    manapun (bukan wajib tiap post — nempel di semua kesannya spam), panjang
    tetep dicek per-post sesuai limit service-nya."""
    errs = []
    if service == "twitter" and "#" not in " ".join(posts):
        errs.append("thread WAJIB ada hashtag di salah satu post")
    for i, t in enumerate(posts, start=1):
        if not t.strip():
            errs.append(f"post #{i} kosong")
            continue
        n = effective_length(service, t)
        if n > LIMITS[service]:
            errs.append(f"post #{i}: {n} char (efektif), maks {service} {LIMITS[service]}")
    return errs


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
    campaign = args.campaign or ("manual" if args.now else args.slot)
    thread_rest = None

    if args.thread:
        posts = [args.text, *args.thread]
        posts[-1] = with_promo_link(args.service, posts[-1], campaign)
        errs = validate_thread(args.service, posts)
        if errs:
            print("DITOLAK:\n- " + "\n- ".join(errs), file=sys.stderr)
            return 1
        text, thread_rest = posts[0], posts[1:]
    else:
        text = with_promo_link(args.service, args.text, campaign)
        errs = validate_text(args.service, text)
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
        print(f"Buffer nolak: {res['message']}", file=sys.stderr)
        return 1
    p = res["post"]
    when = (f"{parse_due(p['dueAt']).astimezone(WIB):%d %b %H:%M} WIB"
            if p.get("dueAt") else "SEKARANG")
    suffix = f" (thread, {1 + len(thread_rest)} tweet)" if thread_rest else ""
    print(f"OK {args.service} post {p['id']} -> {when}{suffix}")
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


def cmd_relink(args) -> int:
    """Tempel link UTM ke post yang UDAH kejadwal (dibuat sebelum fitur ini ada).
    Cuma aman buat status scheduled/draft — post yang sent nggak bisa diedit lagi."""
    chans = {c["id"]: c["service"] for c in get_channels()}
    posts = fetch_posts(50)
    target = next((p for p in posts if p["id"] == args.id), None)
    if not target:
        print(f"post {args.id} nggak ketemu (cek `recent` buat ID)", file=sys.stderr)
        return 1
    if target["status"] not in ("scheduled", "draft"):
        print(f"post status '{target['status']}' — cuma scheduled/draft yang bisa direlink",
              file=sys.stderr)
        return 1
    base_text = args.text or target["text"]
    text = with_promo_link(target["service"], base_text, args.campaign)
    if text == target["text"]:
        print("post ini udah punya link UTM, nggak diapa-apain")
        return 0
    errs = validate_text(target["service"], text)
    if errs:
        print("DITOLAK:\n- " + "\n- ".join(errs), file=sys.stderr)
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
        print(f"Buffer nolak: {res['message']}", file=sys.stderr)
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

    sub.add_parser("channels", help="list channel Buffer")

    p = sub.add_parser("post", help="bikin post terjadwal")
    p.add_argument("--service", choices=SERVICES, required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--topic", default=None, help=f"topic Threads (default: {DEFAULT_TOPIC})")
    p.add_argument("--slot", choices=list(SLOTS), default="pagi",
                   help="jadwal ke slot primetime berikutnya (default: pagi)")
    p.add_argument("--at", default=None, help="override dueAt ISO8601 UTC (jarang perlu)")
    p.add_argument("--now", action="store_true", help="terbit SEKARANG (shareNow), skip slot")
    p.add_argument("--campaign", default=None, help="override utm_campaign (default: nama slot)")
    p.add_argument("--thread", action="append", default=None,
                   help="post lanjutan (twitter & threads) — ulang --thread per post, urut. "
                        "Link CTA nempel di post TERAKHIR, bukan yang pertama.")
    p.add_argument("--dry", action="store_true", help="print payload doang, nggak ngepost")

    r = sub.add_parser("recent", help="post terakhir (anti-repeat / inspirasi)")
    r.add_argument("-n", type=int, default=10)

    v = sub.add_parser("verify", help="verifier loop: slot kejadwal di kedua channel?")
    v.add_argument("--slot", choices=list(SLOTS), required=True)

    rl = sub.add_parser("relink", help="tempel link UTM ke post scheduled yang belum punya")
    rl.add_argument("--id", required=True)
    rl.add_argument("--campaign", required=True)
    rl.add_argument("--text", default=None,
                    help="ganti teks dasar sebelum link ditempel (buat post lama yang kepanjangan)")

    args = ap.parse_args(argv)
    return {"channels": cmd_channels, "post": cmd_post, "relink": cmd_relink,
            "recent": cmd_recent, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
