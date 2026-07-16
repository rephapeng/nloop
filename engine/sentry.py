"""Tutup siklus issue-fix: mark issue Sentry 'resolved' setelah run sukses.

Dipanggil di akhir loop kalau run-nya lahir dari webhook Sentry (fingerprint
`sentry:<issue_id>`) dan `triggers.sentry.resolve: true`. Token dari env
SENTRY_AUTH_TOKEN (.env) — butuh scope `event:write` / `issue admin`.
Gagal resolve nggak menggagalkan run (fix-nya udah kedeploy), cuma warning.
"""
from __future__ import annotations

import os

import httpx


async def resolve_issue(fingerprint: str | None, cfg: dict,
                        transport: httpx.AsyncBaseTransport | None = None,
                        ) -> tuple[str, str] | None:
    """Return (level, msg) buat event log, atau None kalau nggak ada yang perlu
    dilakukan (bukan issue sentry / fitur mati)."""
    s = (cfg.get("triggers") or {}).get("sentry") or {}
    if not s.get("resolve") or not (fingerprint or "").startswith("sentry:"):
        return None
    issue_id = fingerprint.split(":", 1)[1]
    token = os.environ.get("SENTRY_AUTH_TOKEN", "").strip()
    if not token:
        return ("warn", "triggers.sentry.resolve aktif tapi SENTRY_AUTH_TOKEN kosong di .env")

    url = f"{(s.get('url') or 'https://sentry.io').rstrip('/')}/api/0/issues/{issue_id}/"
    try:
        async with httpx.AsyncClient(timeout=30, transport=transport) as client:
            r = await client.put(url, headers={"Authorization": f"Bearer {token}"},
                                 json={"status": "resolved"})
        if r.status_code < 300:
            return ("info", f"issue sentry {issue_id} di-mark resolved ✅")
        return ("warn", f"gagal resolve issue sentry {issue_id}: HTTP {r.status_code} "
                        f"{r.text[:200]}")
    except httpx.HTTPError as e:
        return ("warn", f"gagal resolve issue sentry {issue_id}: {e}")
