"""Workspace: satu proses nloop, banyak "tenant".

Tiap workspace punya tasks/schedules/triggers/watchdog/bot SENDIRI, tapi share
satu proses, satu queue, satu semaphore, dan satu DB. Kenapa share: guardrail
resource utama nloop itu `loops.max_concurrent` (tiap loop = pohon subprocess
claude) dan SATU langganan Claude — dua proses nloop bakal rebutan dua-duanya.
Jadi workspace = NAMESPACE di dalam satu proses, bukan deployment terpisah.

Layout:

    config.yaml                        # level proses: server, loops, claude, paths
    workspaces/onecookie/config.yaml   # level tenant: tasks, schedules, triggers, ...
    workspaces/onecookie/tasks/        # opsional — nimpa paths.tasks
    workspaces/onecookie/roles/        # opsional — nimpa paths.roles
    workspaces/jetorbit/config.yaml

Config workspace di-overlay DI ATAS config global (shallow per-section, sama
kayak `config.load`), jadi workspace bisa nurunin limitnya sendiri (mis.
`loops.max_cost_usd`) tanpa nulis ulang sisanya. `server` dikunci — itu level
proses. DB juga tetap satu; isolasi run lewat kolom `runs.workspace`, dan semua
lookup fingerprint di-scope per workspace (dua workspace boleh punya schedule
sama namanya tanpa saling nge-dedup).

Belum ada direktori `workspaces/` (atau isinya invalid semua)? Balik ke mode
lama: satu workspace implisit bernama `default` isinya persis config global —
semua setup pre-workspace jalan apa adanya.
"""
from __future__ import annotations

import logging
import os
import re

import yaml

from engine import tasks

log = logging.getLogger("nloop.workspaces")

DEFAULT = "default"        # workspace implisit kalau workspaces/ kosong
CONFIG_NAME = "config.yaml"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# Section level-proses: workspace nggak boleh nimpa (satu server, satu DB).
LOCKED = ("server",)


def _copy(cfg: dict) -> dict:
    """Salinan per-section — tiap workspace ngutak-atik dict-nya sendiri."""
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in cfg.items()}


def root_dir(cfg: dict) -> str:
    return (cfg.get("paths") or {}).get("workspaces") or "workspaces"


def _discover(root: str) -> list[str]:
    """Nama workspace = subdirektori yang punya config.yaml."""
    if not os.path.isdir(root):
        return []
    names = []
    for entry in sorted(os.listdir(root)):
        if not os.path.isfile(os.path.join(root, entry, CONFIG_NAME)):
            continue
        if not NAME_RE.match(entry):
            log.error("workspace '%s' namanya invalid (huruf kecil/angka/-/_), di-skip",
                      entry)
            continue
        names.append(entry)
    return names


def _load_one(cfg: dict, root: str, name: str) -> dict:
    path = os.path.join(root, name, CONFIG_NAME)
    with open(path) as f:
        user = yaml.safe_load(f) or {}
    if not isinstance(user, dict):
        raise ValueError(f"{path} bukan mapping")

    ws = _copy(cfg)
    for section, values in user.items():
        if section in LOCKED:
            log.warning("workspace '%s': section '%s' level proses — diabaikan",
                        name, section)
            continue
        if isinstance(values, dict) and isinstance(ws.get(section), dict):
            ws[section] = {**ws[section], **values}
        else:
            ws[section] = values

    # tasks/ dan roles/ di dalam direktori workspace menang atas yang global —
    # definisi kerjaan ikut tenant-nya, bukan numpuk di satu folder bersama.
    ws["paths"] = dict(ws.get("paths") or {})
    for key in ("tasks", "roles"):
        local = os.path.join(root, name, key)
        if os.path.isdir(local):
            ws["paths"][key] = local

    ws["workspace"] = name
    ws["tasks"] = tasks.load_registry(ws)
    return ws


def load_all(cfg: dict) -> dict[str, dict]:
    """{nama: cfg lengkap} — satu cfg utuh per workspace, siap dipakai komponen
    yang udah ada (Scheduler/Watchdog/TelegramBot terima cfg apa adanya)."""
    root = root_dir(cfg)
    out: dict[str, dict] = {}
    for name in _discover(root):
        try:
            out[name] = _load_one(cfg, root, name)
        except (OSError, yaml.YAMLError, ValueError) as e:
            log.error("workspace '%s' invalid, di-skip: %s", name, e)
    if not out:  # mode lama (pre-workspace): config global = satu workspace
        ws = _copy(cfg)
        ws["workspace"] = DEFAULT
        ws["tasks"] = tasks.load_registry(ws)
        return {DEFAULT: ws}
    log.info("workspaces: %s", ", ".join(sorted(out)))
    return out


def primary(ws_cfgs: dict[str, dict]) -> str:
    """Workspace default buat request tanpa `?workspace=` — dan yang MENGADOPSI
    run lama (pre-workspace, kolomnya masih NULL). Ditandai `primary: true` di
    config workspace; kalau nggak ada yang nandain dan cuma ada satu workspace,
    ya itu."""
    flagged = [n for n, ws in ws_cfgs.items() if ws.get("primary")]
    if len(flagged) > 1:
        log.error("lebih dari satu workspace `primary: true` (%s) — dipakai '%s'",
                  ", ".join(sorted(flagged)), sorted(flagged)[0])
    if flagged:
        return sorted(flagged)[0]
    if len(ws_cfgs) == 1:
        return next(iter(ws_cfgs))
    return DEFAULT if DEFAULT in ws_cfgs else sorted(ws_cfgs)[0]


def summary(name: str, ws: dict, is_primary: bool) -> dict:
    """Ringkasan buat GET /api/workspaces (switcher dashboard)."""
    return {
        "name": name,
        "label": ws.get("label") or name,
        "primary": is_primary,
        "tasks": len(ws.get("tasks") or {}),
        "schedules": len(ws.get("schedules") or {}),
        "projects": sorted((ws.get("triggers") or {}).get("projects") or {}),
        "watchdog": bool((ws.get("watchdog") or {}).get("enabled")),
        "telegram": bool((ws.get("telegram") or {}).get("enabled")),
    }
