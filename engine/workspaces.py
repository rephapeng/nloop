"""Workspace: one nloop process, many "tenants".

Each workspace has its OWN tasks/schedules/triggers/watchdog/bot, but shares one
process, one queue, one semaphore, and one DB. Why share: nloop's main resource
guardrails are `loops.max_concurrent` (each loop is a whole tree of claude
subprocesses) and the SINGLE Claude subscription — two nloop processes would
fight over both. So a workspace is a NAMESPACE inside one process, not a
separate deployment.

Layout:

    config.yaml                        # process level: server, loops, claude, paths
    workspaces/onecookie/config.yaml   # tenant level: tasks, schedules, triggers, ...
    workspaces/onecookie/tasks/        # optional — overrides paths.tasks
    workspaces/onecookie/roles/        # optional — overrides paths.roles
    workspaces/jetorbit/config.yaml

A workspace config is overlaid ON TOP of the global config (shallow per section,
just like `config.load`), so a workspace can lower its own limits (e.g.
`loops.max_cost_usd`) without restating the rest. `server` is locked — that is
process level. The DB stays single too; runs are isolated by the `runs.workspace`
column, and every fingerprint lookup is scoped per workspace (two workspaces may
have a schedule of the same name without deduping each other).

No `workspaces/` directory yet (or everything in it is invalid)? Fall back to the
old mode: one implicit workspace called `default` holding exactly the global
config — every pre-workspace setup keeps working as-is.
"""
from __future__ import annotations

import logging
import os
import re

import yaml

from engine import tasks

log = logging.getLogger("nloop.workspaces")

DEFAULT = "default"        # implicit workspace when workspaces/ is empty
CONFIG_NAME = "config.yaml"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# Process-level sections: a workspace may not override these (one server, one DB).
LOCKED = ("server",)


def _copy(cfg: dict) -> dict:
    """A per-section copy — each workspace mutates its own dict."""
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in cfg.items()}


def root_dir(cfg: dict) -> str:
    return (cfg.get("paths") or {}).get("workspaces") or "workspaces"


def _discover(root: str) -> list[str]:
    """A workspace name is a subdirectory that has a config.yaml."""
    if not os.path.isdir(root):
        return []
    names = []
    for entry in sorted(os.listdir(root)):
        if not os.path.isfile(os.path.join(root, entry, CONFIG_NAME)):
            continue
        if not NAME_RE.match(entry):
            log.error("workspace '%s' has an invalid name (lowercase/digits/-/_), skipped",
                      entry)
            continue
        names.append(entry)
    return names


def _load_one(cfg: dict, root: str, name: str) -> dict:
    path = os.path.join(root, name, CONFIG_NAME)
    with open(path) as f:
        user = yaml.safe_load(f) or {}
    if not isinstance(user, dict):
        raise ValueError(f"{path} is not a mapping")

    ws = _copy(cfg)
    for section, values in user.items():
        if section in LOCKED:
            log.warning("workspace '%s': section '%s' is process level — ignored",
                        name, section)
            continue
        if isinstance(values, dict) and isinstance(ws.get(section), dict):
            ws[section] = {**ws[section], **values}
        else:
            ws[section] = values

    # tasks/ and roles/ inside a workspace directory win over the global ones —
    # work definitions live with their tenant instead of piling into one shared folder.
    ws["paths"] = dict(ws.get("paths") or {})
    for key in ("tasks", "roles"):
        local = os.path.join(root, name, key)
        if os.path.isdir(local):
            ws["paths"][key] = local

    ws["workspace"] = name
    ws["paths"]["ws_config"] = path      # second source for tasks.refresh()
    ws["tasks"] = tasks.load_registry(ws)
    return ws


def load_all(cfg: dict) -> dict[str, dict]:
    """{name: full cfg} — one complete cfg per workspace, ready for the existing
    components (Scheduler/Watchdog/TelegramBot take a cfg as-is)."""
    root = root_dir(cfg)
    out: dict[str, dict] = {}
    for name in _discover(root):
        try:
            out[name] = _load_one(cfg, root, name)
        except (OSError, yaml.YAMLError, ValueError) as e:
            log.error("workspace '%s' is invalid, skipped: %s", name, e)
    if not out:  # old mode (pre-workspace): the global config is the one workspace
        ws = _copy(cfg)
        ws["workspace"] = DEFAULT
        ws["tasks"] = tasks.load_registry(ws)
        return {DEFAULT: ws}
    log.info("workspaces: %s", ", ".join(sorted(out)))
    return out


def primary(ws_cfgs: dict[str, dict]) -> str:
    """The default workspace for requests without `?workspace=` — and the one that
    ADOPTS legacy runs (pre-workspace, whose column is still NULL). Marked with
    `primary: true` in a workspace config; if nothing is marked and there is only
    one workspace, that one wins."""
    flagged = [n for n, ws in ws_cfgs.items() if ws.get("primary")]
    if len(flagged) > 1:
        log.error("more than one workspace has `primary: true` (%s) — using '%s'",
                  ", ".join(sorted(flagged)), sorted(flagged)[0])
    if flagged:
        return sorted(flagged)[0]
    if len(ws_cfgs) == 1:
        return next(iter(ws_cfgs))
    return DEFAULT if DEFAULT in ws_cfgs else sorted(ws_cfgs)[0]


def summary(name: str, ws: dict, is_primary: bool) -> dict:
    """Summary for GET /api/workspaces (the dashboard switcher)."""
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
