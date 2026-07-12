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
    },
    "claude": {
        "model": None,
        "max_turns": 30,
        "allowed_tools": "Bash,Read,Edit,Write,Glob,Grep",
        "permission_mode": "acceptEdits",
        "retries": 1,                    # retry per iterasi kalau error transient
        "max_consecutive_errors": 2,     # N iterasi claude error beruntun → fail run
    },
    "memory": {"provider": "local"},
    "paths": {"db": "nloop.db", "workspaces": "workspaces"},
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
