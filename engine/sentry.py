"""Close the issue-fix loop: mark a Sentry issue 'resolved' after a successful run.

Called at the end of the loop when the run came from a Sentry webhook (fingerprint
`sentry:<issue_id>`) and `triggers.sentry.resolve: true`. The token comes from the
SENTRY_AUTH_TOKEN env var (.env) — it needs `event:write` / `issue admin` scope.
A failed resolve never fails the run (the fix already shipped), only warns.
"""
from __future__ import annotations

import os

import httpx


async def resolve_issue(fingerprint: str | None, cfg: dict,
                        transport: httpx.AsyncBaseTransport | None = None,
                        ) -> tuple[str, str] | None:
    """Return (level, msg) for the event log, or None when there is nothing to do
    (not a sentry issue / feature disabled)."""
    s = (cfg.get("triggers") or {}).get("sentry") or {}
    if not s.get("resolve") or not (fingerprint or "").startswith("sentry:"):
        return None
    issue_id = fingerprint.split(":", 1)[1]
    token = os.environ.get("SENTRY_AUTH_TOKEN", "").strip()
    if not token:
        return ("warn", "triggers.sentry.resolve is on but SENTRY_AUTH_TOKEN is empty in .env")

    url = f"{(s.get('url') or 'https://sentry.io').rstrip('/')}/api/0/issues/{issue_id}/"
    try:
        async with httpx.AsyncClient(timeout=30, transport=transport) as client:
            r = await client.put(url, headers={"Authorization": f"Bearer {token}"},
                                 json={"status": "resolved"})
        if r.status_code < 300:
            return ("info", f"sentry issue {issue_id} marked resolved ✅")
        return ("warn", f"failed to resolve sentry issue {issue_id}: HTTP {r.status_code} "
                        f"{r.text[:200]}")
    except httpx.HTTPError as e:
        return ("warn", f"failed to resolve sentry issue {issue_id}: {e}")
