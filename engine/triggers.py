"""Reactive triggers: payload webhook (Sentry/PostHog/generic) → goal loop.

Format webhook tiap vendor beda-beda (dan sering berubah), jadi extractor-nya
toleran: nyoba beberapa path yang umum, fallback ke hash judul buat fingerprint.
Fingerprint dipakai dedup — issue sama nggak boleh spawn loop dobel selama
masih ada run aktif (queued/running).

Mode repro-first (default buat issue run): verifier project doang nggak cukup —
error runtime (mayoritas issue Sentry) nggak bikin build merah, loop bakal
"selesai" tanpa ngapa-ngapain. Makanya verify_cmd issue run digabung dengan
script repro yang WAJIB ditulis agent dulu: file belum ada → verifier gagal →
loop dipaksa ACT (investigasi + tulis repro + fix), dan "selesai" berarti
repro lolos DAN health check project lolos.
"""
from __future__ import annotations

import hashlib
import re

REPRO_DIR = ".nloop/repro"


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


def repro_path(fingerprint: str) -> str:
    """Path script repro per-issue di dalam workdir project (relatif)."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", fingerprint)
    return f"{REPRO_DIR}/{safe}.sh"


def compose_verify(project_verify_cmd: str, rpath: str) -> str:
    """Verifier issue run = repro DULU baru health check project.
    File repro belum ada → `sh` exit 127 → verifier gagal → loop dipaksa ACT."""
    return f"sh {rpath} && ({project_verify_cmd})"


def create_issue_run(store, cfg: dict, proj: dict, source: str, issue: dict) -> str:
    """Spawn satu issue-fix run dari issue ternormalisasi — jalur bersama
    webhook (push) dan watchdog (poll), biar perilakunya identik."""
    verify_cmd = proj["verify_cmd"]
    rpath = None
    if proj.get("repro", True):
        rpath = repro_path(issue["fingerprint"])
        verify_cmd = compose_verify(proj["verify_cmd"], rpath)
    return store.create_run(
        build_goal(source, issue, repro_path=rpath, verify_cmd=verify_cmd),
        verify_cmd,
        proj["workdir"],
        model=proj.get("model") or cfg["claude"].get("model"),
        max_iterations=proj.get("max_iterations") or cfg["loops"]["max_iterations"],
        max_cost_usd=proj.get("max_cost_usd") or cfg["loops"]["max_cost_usd"],
        fingerprint=issue["fingerprint"],
        role=proj.get("role"),
        context_cmd=proj.get("context_cmd"),
        gate_prompt=proj.get("gate_prompt"),
        on_success_cmd=proj.get("on_success_cmd"),
    )


def build_goal(source: str, issue: dict, *, repro_path: str | None = None,
               verify_cmd: str | None = None) -> str:
    lines = [
        f"Issue baru masuk dari {source}: {issue['title']}",
    ]
    if issue["url"]:
        lines.append(f"Link issue: {issue['url']}")
    if issue["detail"]:
        lines.append(f"Detail: {issue['detail']}")
    if repro_path:
        lines += [
            "",
            "Kerjakan sebagai issue-fix loop:",
            "1. INVESTIGASI: baca stacktrace/judul di atas, telusuri kode terkait "
            "di working directory ini sampai ketemu root cause-nya.",
            f"2. REPRO: tulis script `{repro_path}` yang MEREPRODUKSI error ini — "
            "exit != 0 selama bug masih ada, exit 0 setelah bener. Harus se-spesifik "
            "mungkin ke error ini (unit test / skenario nyata), BUKAN placeholder "
            "`exit 0` — verifier repro yang bohong = issue balik lagi dari produksi.",
            "3. FIX: perbaiki root cause-nya di kode, bukan sekadar bikin repro lolos.",
            f"4. Selesai/tidaknya ditentukan verifier eksternal: `{verify_cmd}` — "
            "script repro DAN health check project dua-duanya harus lolos.",
        ]
    else:
        lines.append(
            "Investigasi root cause error ini di project (working directory ini), "
            "lalu perbaiki sampai verifier lolos. Kalau butuh, tulis test reproduksi dulu."
        )
    return "\n".join(lines)
