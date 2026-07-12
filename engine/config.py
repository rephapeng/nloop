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
    },
    "claude": {
        "model": None,
        "max_turns": 30,
        "allowed_tools": "Bash,Read,Edit,Write,Glob,Grep",
        "permission_mode": "acceptEdits",
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
