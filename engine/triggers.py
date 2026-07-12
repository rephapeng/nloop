"""Reactive triggers: payload webhook (Sentry/PostHog/generic) → goal loop.

Format webhook tiap vendor beda-beda (dan sering berubah), jadi extractor-nya
toleran: nyoba beberapa path yang umum, fallback ke hash judul buat fingerprint.
Fingerprint dipakai dedup — issue sama nggak boleh spawn loop dobel selama
masih ada run aktif (queued/running).
"""
from __future__ import annotations

import hashlib


def _dig(d: dict, *paths: str):
    """Ambil nilai pertama yang ketemu dari beberapa dotted-path."""
    for path in paths:
        cur = d
        found = True
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                found = False
                break
        if found and cur not in (None, "", {}):
            return cur
    return None


def extract_issue(source: str, payload: dict) -> dict:
    """Normalisasi payload webhook → {fingerprint, title, url, detail}."""
    if source == "sentry":
        fp = _dig(payload, "data.issue.id", "data.event.issue_id", "issue_id", "id")
        title = _dig(payload, "data.issue.title", "data.event.title",
                     "event.title", "message", "title")
        url = _dig(payload, "data.issue.web_url", "data.event.web_url",
                   "data.issue.url", "url")
        detail = _dig(payload, "data.event.culprit", "data.issue.culprit",
                      "culprit", "data.issue.metadata.value")
    elif source == "posthog":
        fp = _dig(payload, "issue_id", "event.uuid", "uuid", "id")
        title = _dig(payload, "issue_name", "title",
                     "event.properties.$exception_message", "event.event", "message")
        url = _dig(payload, "issue_url", "url", "event.url")
        detail = _dig(payload, "description",
                      "event.properties.$exception_type", "detail")
    else:  # generic — bisa dipakai curl manual / vendor lain
        fp = _dig(payload, "fingerprint", "issue_id", "id")
        title = _dig(payload, "title", "message", "name")
        url = _dig(payload, "url")
        detail = _dig(payload, "detail", "description")

    title = str(title) if title else "(untitled issue)"
    if not fp:  # tanpa id → fingerprint dari judul, biar dedup tetap jalan
        fp = hashlib.sha1(f"{source}:{title}".encode()).hexdigest()[:16]
    return {
        "fingerprint": f"{source}:{fp}",
        "title": title,
        "url": str(url) if url else "",
        "detail": str(detail) if detail else "",
    }


def build_goal(source: str, issue: dict) -> str:
    lines = [
        f"Issue baru masuk dari {source}: {issue['title']}",
    ]
    if issue["url"]:
        lines.append(f"Link issue: {issue['url']}")
    if issue["detail"]:
        lines.append(f"Detail: {issue['detail']}")
    lines.append(
        "Investigasi root cause error ini di project (working directory ini), "
        "lalu perbaiki sampai verifier lolos. Kalau butuh, tulis test reproduksi dulu."
    )
    return "\n".join(lines)
