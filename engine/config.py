"""Load config.yaml + defaults. Semua akses config lewat sini."""
from __future__ import annotations

import os

import yaml

DEFAULTS = {
    "server": {"host": "127.0.0.1", "port": 8484},
    "loops": {
        "max_concurrent": 2,
        "max_iterations": 10,
        "max_cost_usd": 5.0,
        "iteration_timeout_sec": 900,
        "poll_interval_sec": 1.0,
        "max_no_progress": 2,        # N verifier-output identik beruntun → stop
        "budget_warn_ratio": 0.8,    # emit warning saat cost nyentuh 80% budget
        "postrun_timeout_sec": 600,  # timeout on_success_cmd (push/deploy)
    },
    "claude": {
        "model": None,
        "max_turns": 30,
        "allowed_tools": "Bash,Read,Edit,Write,Glob,Grep",
        "permission_mode": "acceptEdits",
        "retries": 1,                    # retry per iterasi kalau error transient
        "max_consecutive_errors": 2,     # N iterasi claude error beruntun → fail run
        "lock_file": None,               # single-flight LINTAS proses (flock) — samain
                                         # dengan agent lain yang share subscription
                                         # (pola .claude.lock dtc-agent)
        "gate_max_turns": 15,            # LLM gate: reviewer read-only, murah
        "gate_allowed_tools": "Read,Grep,Glob",
    },
    "memory": {"provider": "local"},
    "triggers": {
        "token": None,
        "projects": {},
        # tutup siklus issue-fix: run sukses → mark resolved di Sentry
        # (token dari .env: SENTRY_AUTH_TOKEN)
        "sentry": {"resolve": False, "url": "https://sentry.io"},
    },
    "schedules": {},                     # loop terjadwal (port systemd-timer dtc), contoh di config.yaml
    "watchdog": {                        # poll Sentry cari issue unresolved → spawn loop
        "enabled": False,
        "interval": "5m",
        "cooldown": "24h",               # jangan respawn issue yang barusan dicoba
        "max_per_tick": 2,
        "organization": None,            # slug org Sentry (WAJIB kalau enabled)
        "projects": {},                  # sentry project slug -> nama di triggers.projects
        "query": "is:unresolved",
    },
    "telegram": {                        # bot Telegram: notif + kontrol + chat agent
        "enabled": False,                # token & chat id dari .env / env var
        "notify": True,                  # notif run selesai (succeeded/failed/stopped)
        "agent_workdir": ".",            # cwd chat freeform → session claude
        "model": None,                   # model chat substantif (None = default CLI)
        "model_smalltalk": "sonnet",     # sapaan pendek → tier murah (pola agent_run.sh)
        "thinking_tokens": 10000,        # budget thinking buat pesan substantif
        "cmd_timeout_sec": 900,
        "max_turns": None,               # None = tanpa batas (beda dari claude.max_turns)
        "progress_interval_sec": 60,     # kirim update progres tiap N detik (0 = off)
    },
    "paths": {"db": "nloop.db", "workspaces": "workspaces", "roles": "roles"},
}


def load(path: str = "config.yaml") -> dict:
    """Config = DEFAULTS di-overlay isi config.yaml (kalau ada)."""
    cfg = {section: dict(values) for section, values in DEFAULTS.items()}
    if os.path.exists(path):
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        for section, values in user.items():
            cfg.setdefault(section, {}).update(values or {})
    return cfg


def load_env(path: str = ".env") -> None:
    """Isi os.environ dari .env (KEY=VALUE) TANPA menimpa env beneran.
    Secrets (token Telegram dst.) hidup di sini, JANGAN di config.yaml
    (config.yaml ke-commit). Pola load_env dtc-agent."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
