"""Load config.yaml + defaults. All config access goes through here."""
from __future__ import annotations

import hashlib
import logging
import os

import yaml

log = logging.getLogger("nloop.config")

DEFAULTS = {
    "server": {"host": "127.0.0.1", "port": 8484},
    "loops": {
        "max_concurrent": 2,
        "max_iterations": 10,
        "max_cost_usd": 5.0,
        "iteration_timeout_sec": 900,
        "poll_interval_sec": 1.0,
        "max_no_progress": 2,        # N identical verifier outputs in a row → stop
        "budget_warn_ratio": 0.8,    # emit a warning when cost hits 80% of budget
        "postrun_timeout_sec": 600,  # on_success_cmd timeout (push/deploy)
    },
    "claude": {
        "model": None,
        "max_turns": 30,
        "allowed_tools": "Bash,Read,Edit,Write,Glob,Grep",
        "permission_mode": "acceptEdits",
        "retries": 1,                    # retries per iteration on a transient error
        "max_consecutive_errors": 2,     # N claude errors in a row → fail the run
        "lock_file": None,               # CROSS-process single-flight (flock) — point it
                                         # at the same file as other agents sharing the
                                         # subscription (dtc-agent's .claude.lock pattern)
        "gate_max_turns": 15,            # LLM gate: read-only reviewer, cheap
        "gate_allowed_tools": "Read,Grep,Glob",
    },
    "memory": {"provider": "local"},
    "triggers": {
        "token": None,
        "projects": {},
        # closes the issue-fix loop: successful run → mark resolved in Sentry
        # (token from .env: SENTRY_AUTH_TOKEN)
        "sentry": {"resolve": False, "url": "https://sentry.io"},
    },
    "tasks": {},                         # Fase 10: reusable task registry (see engine/tasks.py).
                                         # Also <paths.tasks>/<id>.yaml files — the file wins.
    "schedules": {},                     # scheduled loops (dtc systemd-timer port), examples in config.yaml
    "watchdog": {                        # poll Sentry for unresolved issues → spawn loops
        "enabled": False,
        "interval": "5m",
        "cooldown": "24h",               # don't respawn an issue that was just attempted
        "max_per_tick": 2,
        "organization": None,            # Sentry org slug (REQUIRED when enabled)
        "projects": {},                  # sentry project slug -> name in triggers.projects
        "query": "is:unresolved",
    },
    "telegram": {                        # Telegram bot: notifications + control + agent chat
        "enabled": False,                # token & chat id come from .env / env vars
        "notify": True,                  # notify on terminal runs (succeeded/failed/stopped)
        "agent_workdir": ".",            # cwd for freeform chat → claude session
        "model": None,                   # model for substantive chat (None = CLI default)
        "model_smalltalk": "sonnet",     # short greetings → cheap tier (agent_run.sh pattern)
        "thinking_tokens": 10000,        # thinking budget for substantive messages
        "cmd_timeout_sec": 900,
        "max_turns": None,               # None = unlimited (unlike claude.max_turns)
        "progress_interval_sec": 60,     # send a progress update every N seconds (0 = off)
    },
    "promo_report": {                    # daily promo traffic report -> Telegram
        "enabled": False,                # also needs telegram.enabled (it sends via the bot)
        "at": "14:30",                   # UTC = 21:30 WIB, after the evening slot and most of the day
        "days": 7,                       # window for aggregates/trends/breakdown (Today & Yesterday always shown)
        "send_on_start": False,          # send once when the server boots instead of waiting for the schedule
    },
    # workspaces: tenant config directory (workspaces/<name>/config.yaml — see
    # engine/workspaces.py). scratch: throwaway workdir for runs that bring no
    # workdir of their own — this USED to mean `paths.workspaces`, split apart when
    # a workspace became a tenant concept.
    "paths": {"db": "nloop.db", "workspaces": "workspaces", "scratch": ".scratch",
              "roles": "roles", "tasks": "tasks"},
}


def scratch_dir(cfg: dict) -> str:
    """Throwaway workdir directory. Older configs that only set `paths.workspaces`
    still work through the fallback below."""
    paths = cfg.get("paths") or {}
    return paths.get("scratch") or paths.get("workspaces") or ".scratch"


def load(path: str = "config.yaml") -> dict:
    """Config = DEFAULTS overlaid with config.yaml (when present)."""
    cfg = {section: dict(values) for section, values in DEFAULTS.items()}
    if os.path.exists(path):
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        for section, values in user.items():
            cfg.setdefault(section, {}).update(values or {})
    # Recorded so the task registry can re-read this file when it changes
    # (see tasks.refresh) — adding a task needs no server restart.
    cfg["paths"] = {**cfg.get("paths", {}), "config": path}
    return cfg


# ---- config files as a live source ----
# `paths.config` / `paths.ws_config` record which YAML files a workspace cfg was
# built from, so components can re-read a section when the file changes instead of
# demanding `systemctl restart nloop`. Used by tasks.refresh() and the Scheduler.

SOURCE_KEYS = ("config", "ws_config")
RELOAD_INTERVAL_SEC = 30   # how often live components re-read their config section


def source_files(cfg: dict) -> list[str]:
    """Existing config files behind this cfg, global first then workspace."""
    paths = cfg.get("paths") or {}
    return [paths[k] for k in SOURCE_KEYS
            if paths.get(k) and os.path.exists(paths[k])]


def file_sig(path: str) -> str:
    """Content hash. Deliberately not (mtime, size): an in-place edit of the same
    length inside one filesystem timestamp tick is invisible to mtime."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return ""


def section_from_disk(cfg: dict, section: str) -> dict:
    """Re-read one top-level section from the config files (workspace wins),
    mirroring how workspaces._load_one merged it at boot."""
    out: dict = {}
    for path in source_files(cfg):
        try:
            with open(path) as f:
                doc = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            log.error("config %s unreadable during reload, skipped: %s", path, e)
            continue
        if not isinstance(doc, dict):
            continue
        for key, value in (doc.get(section) or {}).items():
            out[str(key)] = value
    return out


def load_env(path: str = ".env") -> None:
    """Fill os.environ from .env (KEY=VALUE) WITHOUT overwriting real env vars.
    Secrets (Telegram token etc.) live here, NEVER in config.yaml
    (config.yaml is committed). dtc-agent's load_env pattern."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
