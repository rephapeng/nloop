"""Watchdog Sentry: poll API cari issue unresolved → spawn issue-fix run.

Pelengkap webhook (push). Webhook butuh alert rule kepasang + server kejangkau
dari Sentry; watchdog cuma butuh koneksi keluar — dia narik sendiri daftar issue
unresolved tiap interval, jadi issue TETAP ketangkep walau alert rule lupa
dipasang atau webhook-nya sempat down.

Tiap project punya loop poll SENDIRI-SENDIRI (pola sama kayak scheduler: satu
asyncio task per entry), jadi interval bisa beda per app:

    watchdog:
      interval: 1h              # default kalau project nggak override
      projects:
        marginin: marginin      # bentuk pendek — pakai interval default
        onecookie:
          name: onecookie
          interval: 2h           # override khusus project ini

Guardrail spawn:
- dedup jalur webhook: fingerprint `sentry:<id>` aktif → skip
- cooldown: fingerprint yang run terakhirnya baru selesai (sukses ATAU gagal)
  di dalam jendela cooldown → skip — issue bandel jangan bakar budget tiap tick
- max_per_tick: cap spawn per putaran (bisa di-override per project juga)

Auth: SENTRY_AUTH_TOKEN dari .env (sama dengan auto-resolve).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from engine import triggers
from engine.scheduler import parse_every

log = logging.getLogger("nloop.watchdog")


def _entry_name(entry) -> str:
    """Entry `projects:` bisa string pendek atau dict dengan override."""
    return entry["name"] if isinstance(entry, dict) else entry


def _entry_interval(entry, default: str) -> str:
    if isinstance(entry, dict) and entry.get("interval"):
        return str(entry["interval"])
    return default


def _entry_max_per_tick(entry, default: int) -> int:
    if isinstance(entry, dict) and entry.get("max_per_tick") is not None:
        return int(entry["max_per_tick"])
    return default


class Watchdog:
    def __init__(self, store, cfg: dict,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.store = store
        self.cfg = cfg
        self.wcfg = cfg.get("watchdog", {})
        self.transport = transport   # injeksi buat test (MockTransport)
        self._stopping = asyncio.Event()
        # state ringkasan — union dari tick terakhir (manual ATAU project mana pun),
        # dipertahankan buat dashboard lama (GET /api/watchdog)
        self.last_tick_at: float | None = None
        self.last_checked: int = 0
        self.last_spawned: list[str] = []
        self.last_error: str | None = None
        # state per-project — tiap project poll sendiri-sendiri, interval bisa beda
        self.project_status: dict[str, dict] = {}

    def status(self) -> dict:
        w = self.wcfg
        default_interval = w.get("interval", "5m")
        proj_map = w.get("projects") or {}
        return {
            "enabled": bool(w.get("enabled")),
            "interval": default_interval,
            "cooldown": w.get("cooldown", "24h"),
            "organization": w.get("organization"),
            "projects": {slug: _entry_name(entry) for slug, entry in proj_map.items()},
            "project_intervals": {
                slug: _entry_interval(entry, default_interval)
                for slug, entry in proj_map.items()
            },
            "token_set": bool(os.environ.get("SENTRY_AUTH_TOKEN", "").strip()),
            "last_tick_at": self.last_tick_at,
            "last_checked": self.last_checked,
            "last_spawned": self.last_spawned,
            "last_error": self.last_error,
            "project_status": self.project_status,
        }

    # ---- lifecycle ----

    async def run_forever(self) -> None:
        if not self.wcfg.get("enabled"):
            await self._stopping.wait()
            return
        if not self.wcfg.get("organization"):
            log.error("watchdog aktif tapi watchdog.organization kosong — mati")
            await self._stopping.wait()
            return
        proj_map = self.wcfg.get("projects") or {}
        default_interval = self.wcfg.get("interval", "5m")
        tasks = []
        for sentry_slug, entry in proj_map.items():
            interval = parse_every(_entry_interval(entry, default_interval))
            log.info("watchdog[%s] aktif: tiap %ss -> %s",
                     sentry_slug, interval, _entry_name(entry))
            tasks.append(asyncio.create_task(
                self._run_project(sentry_slug, entry, interval)))
        if not tasks:
            await self._stopping.wait()
            return
        await self._stopping.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_project(self, sentry_slug: str, entry, interval: float) -> None:
        """Loop poll independen satu project — interval sendiri, guardrail sendiri."""
        while not self._stopping.is_set():
            try:
                spawned = await self._tick_project(sentry_slug, entry)
                if spawned:
                    log.info("watchdog[%s] spawn %d run: %s",
                             sentry_slug, len(spawned), spawned)
            except Exception:  # tick satu project meledak ≠ project lain ikut mati
                log.exception("watchdog[%s] tick error", sentry_slug)
            if not await self._sleep(interval):
                return

    async def stop(self) -> None:
        self._stopping.set()

    # ---- satu putaran manual (semua project sekaligus — /api/watchdog/tick) ----

    async def tick(self) -> list[str]:
        self.last_tick_at = time.time()
        self.last_checked = 0
        self.last_spawned = []
        self.last_error = None

        token = os.environ.get("SENTRY_AUTH_TOKEN", "").strip()
        if not token:
            log.warning("watchdog: SENTRY_AUTH_TOKEN kosong — tick di-skip")
            self.last_error = "SENTRY_AUTH_TOKEN kosong di .env"
            return []

        cooldown = parse_every(self.wcfg.get("cooldown", "24h"))
        max_per_tick = self.wcfg.get("max_per_tick", 2)
        proj_map = self.wcfg.get("projects") or {}
        spawned = self.last_spawned

        for sentry_slug, entry in proj_map.items():
            proj, issues, err = await self._project_issues(sentry_slug, entry, token)
            if err:
                self.last_error = err
                continue
            self.last_checked += len(issues)
            spawned.extend(self._spawn_from_issues(
                proj, issues, cooldown, max_per_tick - len(spawned)))
            if len(spawned) >= max_per_tick:
                break
        return spawned

    # ---- satu putaran per-project (dipakai background loop, interval sendiri) ----

    async def _tick_project(self, sentry_slug: str, entry) -> list[str]:
        now = time.time()
        self.last_tick_at = now
        st = self.project_status.setdefault(sentry_slug, {})
        st.update(last_tick_at=now, last_checked=0, last_spawned=[], last_error=None)

        token = os.environ.get("SENTRY_AUTH_TOKEN", "").strip()
        if not token:
            st["last_error"] = self.last_error = "SENTRY_AUTH_TOKEN kosong di .env"
            return []

        proj, issues, err = await self._project_issues(sentry_slug, entry, token)
        if err:
            st["last_error"] = self.last_error = err
            return []
        st["last_checked"] = self.last_checked = len(issues)

        cooldown = parse_every(self.wcfg.get("cooldown", "24h"))
        max_per_tick = _entry_max_per_tick(entry, self.wcfg.get("max_per_tick", 2))
        spawned = self._spawn_from_issues(proj, issues, cooldown, max_per_tick)
        st["last_spawned"] = self.last_spawned = spawned
        return spawned

    # ---- helper bersama ----

    async def _project_issues(self, sentry_slug: str, entry, token: str,
                              ) -> tuple[dict | None, list[dict], str | None]:
        """Return (proj_cfg, issues, error_msg) — error_msg None kalau sukses."""
        nloop_name = _entry_name(entry)
        proj = (self.cfg.get("triggers", {}).get("projects") or {}).get(nloop_name)
        if proj is None:
            msg = f"project '{nloop_name}' tidak ada di triggers.projects"
            log.error("watchdog: %s — skip", msg)
            return None, [], msg
        try:
            issues = await self._fetch_issues(sentry_slug, token)
        except httpx.HTTPError as e:
            msg = f"fetch {sentry_slug}: {e}"
            log.warning("watchdog: gagal %s", msg)
            return proj, [], msg
        return proj, issues, None

    def _spawn_from_issues(self, proj: dict | None, issues: list[dict],
                           cooldown: float, budget: int) -> list[str]:
        """Dedup + cooldown + spawn, maksimal `budget` run baru."""
        spawned: list[str] = []
        if proj is None:
            return spawned
        for it in issues:
            if len(spawned) >= budget:
                break
            issue = self._normalize(it)
            fp = issue["fingerprint"]
            if self.store.find_active_by_fingerprint(fp):
                continue                                   # masih dikerjain
            last = self.store.last_run_for_fingerprint(fp)
            if last:
                ref = last["ended_at"] or last["created_at"] or 0
                if time.time() - ref < cooldown:
                    continue                               # cooldown — jangan spam
            run_id = triggers.create_issue_run(
                self.store, self.cfg, proj, "sentry", issue)
            log.info("watchdog: issue %s (%s) → run %s",
                     fp, issue["title"][:80], run_id)
            spawned.append(run_id)
        return spawned

    async def _fetch_issues(self, project_slug: str, token: str) -> list[dict]:
        s = self.cfg.get("triggers", {}).get("sentry") or {}
        base = (s.get("url") or "https://sentry.io").rstrip("/")
        org = self.wcfg["organization"]
        url = f"{base}/api/0/projects/{org}/{project_slug}/issues/"
        params = {"query": self.wcfg.get("query", "is:unresolved"),
                  "statsPeriod": "24h"}
        async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
            r = await client.get(url, params=params,
                                 headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            data = r.json()
        return data if isinstance(data, list) else []

    @staticmethod
    def _normalize(it: dict) -> dict:
        """Issue API Sentry → bentuk issue yang sama dengan extractor webhook."""
        return {
            "fingerprint": f"sentry:{it.get('id')}",
            "title": str(it.get("title") or "(untitled issue)"),
            "url": str(it.get("permalink") or ""),
            "detail": str(it.get("culprit") or ""),
        }

    async def _sleep(self, sec: float) -> bool:
        try:
            await asyncio.wait_for(self._stopping.wait(), sec)
            return False
        except TimeoutError:
            return True
