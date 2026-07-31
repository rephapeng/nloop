"""Sentry watchdog: poll the API for unresolved issues → spawn issue-fix runs.

The counterpart to the webhook (push). A webhook needs an alert rule wired up and a
server Sentry can reach; the watchdog only needs outbound connectivity — it pulls the
list of unresolved issues itself every interval, so an issue is STILL caught when the
alert rule was never set up or the webhook was down for a while.

Each project gets its OWN poll loop (same pattern as the scheduler: one asyncio task
per entry), so intervals can differ per app:

    watchdog:
      interval: 1h              # default when a project doesn't override it
      projects:
        marginin: marginin      # short form — uses the default interval
        onecookie:
          name: onecookie
          interval: 2h           # override for this project only

Spawn guardrails:
- webhook-path dedup: an active `sentry:<id>` fingerprint → skip
- cooldown: a fingerprint whose last run just finished (succeeded OR failed) inside
  the cooldown window → skip — a stubborn issue must not burn budget every tick
- max_per_tick: cap on spawns per round (also overridable per project)

Auth: SENTRY_AUTH_TOKEN from .env (the same one auto-resolve uses).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from engine import config, triggers
from engine.scheduler import parse_every

log = logging.getLogger("nloop.watchdog")


def _entry_name(entry) -> str:
    """A `projects:` entry may be a short string or a dict with overrides."""
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
        self.workspace = cfg.get("workspace")
        self.wcfg = cfg.get("watchdog", {})
        self.transport = transport   # injected for tests (MockTransport)
        self._stopping = asyncio.Event()
        self._running: dict[str, tuple] = {}   # sentry slug -> ((entry, interval), task)
        self._complained: set[str] = set()      # config errors already logged once
        # summary state — the union of the last tick (manual OR any project),
        # kept for the legacy dashboard (GET /api/watchdog)
        self.last_tick_at: float | None = None
        self.last_checked: int = 0
        self.last_spawned: list[str] = []
        self.last_error: str | None = None
        # per-project state — each project polls on its own, intervals may differ
        self.project_status: dict[str, dict] = {}

    def status(self) -> dict:
        w = self.wcfg
        default_interval = w.get("interval", "5m")
        proj_map = w.get("projects") or {}
        return {
            "workspace": self.workspace,
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
        """Supervise one poll task per project, re-reading `watchdog:` (and
        `triggers:`, which holds the project mapping the watchdog resolves against)
        from the config files every config.RELOAD_INTERVAL_SEC.

        Adding a project used to require `systemctl restart nloop`, which is a blunt
        instrument: a restart also drops SSE streams, requeues running loops, and
        kills the Telegram chat session. The supervisor runs even when the watchdog
        is disabled at boot, so turning it on later takes effect on its own too.
        """
        self._sync(self.wcfg)
        while not self._stopping.is_set():
            if not await self._sleep(config.RELOAD_INTERVAL_SEC):
                break
            self._sync(self._from_disk())
        for _key, task in self._running.values():
            task.cancel()
        await asyncio.gather(*(t for _k, t in self._running.values()),
                             return_exceptions=True)

    def _from_disk(self) -> dict:
        """`watchdog:` re-read from the config files, with `triggers:` refreshed
        alongside it — a new watchdog project is useless until its counterpart in
        `triggers.projects` exists. A hand-built cfg (library use, tests) has no file
        behind it, so keep whatever is in memory."""
        if not config.source_files(self.cfg):
            return self.wcfg
        trig = config.section_from_disk(self.cfg, "triggers")
        if trig:
            self.cfg["triggers"] = {**(self.cfg.get("triggers") or {}), **trig}
        disk = config.section_from_disk(self.cfg, "watchdog")
        return {**self.wcfg, **disk} if disk else self.wcfg

    def _sync(self, wcfg: dict) -> None:
        """Reconcile poll tasks against `wcfg`: drop removed/changed projects, start
        new ones, leave untouched projects alone (restarting resets their interval)."""
        self.wcfg = self.cfg["watchdog"] = dict(wcfg)
        wanted: dict[str, tuple] = {}
        if wcfg.get("enabled") and wcfg.get("organization"):
            default_interval = wcfg.get("interval", "5m")
            for sentry_slug, entry in (wcfg.get("projects") or {}).items():
                try:
                    interval = parse_every(_entry_interval(entry, default_interval))
                except (ValueError, TypeError) as e:
                    # The supervisor re-reads config every 30s — complain once, not
                    # on every tick.
                    if sentry_slug not in self._complained:
                        self._complained.add(sentry_slug)
                        log.error("watchdog[%s] invalid interval, skipped: %s",
                                  sentry_slug, e)
                    continue
                self._complained.discard(sentry_slug)
                wanted[sentry_slug] = (entry, interval)
        elif wcfg.get("enabled") and not wcfg.get("organization"):
            if "__org__" not in self._complained:
                self._complained.add("__org__")
                log.error("watchdog is enabled but watchdog.organization is empty — idle")
        else:
            self._complained.discard("__org__")

        for slug in list(self._running):
            key, task = self._running[slug]
            if slug in wanted and wanted[slug] == key:
                continue
            task.cancel()
            del self._running[slug]
            self.project_status.pop(slug, None)
            log.info("watchdog[%s] %s", slug,
                     "changed — restarting" if slug in wanted else "removed")

        for slug, (entry, interval) in wanted.items():
            if slug in self._running:
                continue
            self._running[slug] = (
                (entry, interval),
                asyncio.create_task(self._run_project(slug, entry, interval)))
            log.info("watchdog[%s] active: every %ss -> %s",
                     slug, interval, _entry_name(entry))

    async def _run_project(self, sentry_slug: str, entry, interval: float) -> None:
        """An independent poll loop for one project — its own interval and guardrails."""
        while not self._stopping.is_set():
            try:
                spawned = await self._tick_project(sentry_slug, entry)
                if spawned:
                    log.info("watchdog[%s] spawn %d run: %s",
                             sentry_slug, len(spawned), spawned)
            except Exception:  # one project's tick blowing up must not kill the others
                log.exception("watchdog[%s] tick error", sentry_slug)
            if not await self._sleep(interval):
                return

    async def stop(self) -> None:
        self._stopping.set()

    # ---- one manual round (every project at once — /api/watchdog/tick) ----

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

    # ---- one per-project round (used by the background loop, own interval) ----

    async def _tick_project(self, sentry_slug: str, entry) -> list[str]:
        now = time.time()
        self.last_tick_at = now
        st = self.project_status.setdefault(sentry_slug, {})
        st.update(last_tick_at=now, last_checked=0, last_spawned=[], last_error=None)

        token = os.environ.get("SENTRY_AUTH_TOKEN", "").strip()
        if not token:
            st["last_error"] = self.last_error = "SENTRY_AUTH_TOKEN is empty in .env"
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

    # ---- shared helpers ----

    async def _project_issues(self, sentry_slug: str, entry, token: str,
                              ) -> tuple[dict | None, list[dict], str | None]:
        """Return (proj_cfg, issues, error_msg) — error_msg is None on success."""
        nloop_name = _entry_name(entry)
        proj = (self.cfg.get("triggers", {}).get("projects") or {}).get(nloop_name)
        if proj is None:
            msg = f"project '{nloop_name}' is not in triggers.projects"
            log.error("watchdog: %s — skip", msg)
            return None, [], msg
        try:
            issues = await self._fetch_issues(sentry_slug, token)
        except httpx.HTTPError as e:
            msg = f"fetch {sentry_slug}: {e}"
            log.warning("watchdog: failed %s", msg)
            return proj, [], msg
        return proj, issues, None

    def _spawn_from_issues(self, proj: dict | None, issues: list[dict],
                           cooldown: float, budget: int) -> list[str]:
        """Dedup + cooldown + spawn, at most `budget` new runs."""
        spawned: list[str] = []
        if proj is None:
            return spawned
        for it in issues:
            if len(spawned) >= budget:
                break
            issue = self._normalize(it)
            fp = issue["fingerprint"]
            if self.store.find_active_by_fingerprint(fp, workspace=self.workspace):
                continue                                   # still being worked on
            last = self.store.last_run_for_fingerprint(fp, workspace=self.workspace)
            if last:
                ref = last["ended_at"] or last["created_at"] or 0
                if time.time() - ref < cooldown:
                    continue                               # cooldown — don't spam
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
        """A Sentry API issue → the same issue shape the webhook extractor produces."""
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
