"""Task registry: reusable task definitions + payload triggers (trigger.dev style).

Before this, every entry path (REST, scheduler, webhook, Telegram) assembled its
own run — every run was a one-off, there was no "task" you could look at or run
again. Now everything goes through ONE door:

    tasks.trigger(store, cfg, "<task_id>", payload)

A task is defined once under `tasks:` in config.yaml OR in a `tasks/<id>.yaml`
file (one file = one task, id = the file name). Templatable fields ({{...}}) are
filled from the payload at trigger time:

    tasks:
      promo-post:
        name: "Promo post MarginIn"
        payload:
          required: [slot]                # missing → TaskError (fail fast)
          defaults: {n_recent: 10}
        goal: "Write a gimmick post for slot {{slot}} ..."
        verify_cmd: "scripts/buffer_post.py verify --slot {{slot}}"
        workdir: /opt/nloop
        idempotency_key: "promo:{{slot}}"  # dedup: 1 active run per slot

`{{slot}}` and `{{payload.slot}}` both work. A variable that isn't in the payload
is an error at trigger time (not a silently empty string) — a goal with a hole in
it makes the agent invent work.
"""
from __future__ import annotations

import logging
import os
import re
import uuid

import yaml

from engine import config

log = logging.getLogger("nloop.tasks")

# Task fields whose contents get rendered from the payload.
TEMPLATED = ("goal", "verify_cmd", "workdir", "context_cmd", "gate_prompt",
             "on_success_cmd", "idempotency_key")
# Non-template fields handed down to the run as-is.
PASSTHROUGH = ("model", "max_iterations", "max_cost_usd", "role")
# Overrides allowed per-trigger (a payload must not be able to change workdir etc.)
OVERRIDABLE = ("model", "max_iterations", "max_cost_usd", "workdir")

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


class TaskError(ValueError):
    """Task doesn't exist / spec is broken / payload is incomplete."""


# ---- registry ----

def load_registry(cfg: dict, inline: dict | None = None) -> dict[str, dict]:
    """Merge `tasks:` from config.yaml + the `<paths.tasks>/<id>.yaml` files.

    A file in the directory wins over config.yaml ("task as code": the definition
    travels with the repo instead of piling up in one giant config). A broken spec
    is LOGGED and skipped — one typo'd task must not take the server down.

    `inline` = the tasks defined in config (used by `refresh()`, which re-reads
    from disk). Empty → take them from `cfg["tasks"]` like at boot.
    """
    registry: dict[str, dict] = {}
    if inline is None:
        # Snapshot the config tasks BEFORE cfg["tasks"] gets overwritten by the
        # merge — `refresh()` needs the originals, not the merged registry.
        inline = {str(k): dict(v or {}) for k, v in (cfg.get("tasks") or {}).items()}
        cfg[INLINE_KEY] = inline
    for task_id, spec in inline.items():
        registry[str(task_id)] = dict(spec or {})

    tasks_dir = (cfg.get("paths") or {}).get("tasks")
    if tasks_dir and os.path.isdir(tasks_dir):
        for fname in sorted(os.listdir(tasks_dir)):
            if not fname.endswith((".yaml", ".yml")):
                continue
            path = os.path.join(tasks_dir, fname)
            try:
                with open(path) as f:
                    spec = yaml.safe_load(f) or {}
            except (OSError, yaml.YAMLError) as e:
                log.error("task file %s unreadable, skipped: %s", path, e)
                continue
            if not isinstance(spec, dict):
                log.error("task file %s is not a mapping, skipped", path)
                continue
            registry[os.path.splitext(fname)[0]] = spec

    valid = {}
    for task_id, spec in registry.items():
        try:
            validate(task_id, spec)
        except TaskError as e:
            log.error("task '%s' invalid, skipped: %s", task_id, e)
            continue
        valid[task_id] = spec
    if valid:
        log.info("tasks: %d tasks registered", len(valid))
    return valid


# ---- hot reload ----
# Adding or editing a task must not require `systemctl restart nloop`. The registry
# re-reads itself as soon as a source file changes, detected by hashing the contents
# every time the registry is read. Deliberately a content hash and not (mtime, size):
# two edits of the same length inside one filesystem timestamp tick are invisible to
# mtime, and that is exactly what an editor rewriting a file in place looks like.
# The files are few and tiny, so the read costs nothing worth optimising.

STAMP_KEY = "_tasks_stamp"     # fingerprint of the last sources, kept on the workspace cfg
INLINE_KEY = "_tasks_inline"   # tasks from config, snapshotted at boot


def source_files(cfg: dict) -> list[str]:
    """Every file that shapes the registry, in a deterministic order."""
    paths = cfg.get("paths") or {}
    files = config.source_files(cfg)
    tasks_dir = paths.get("tasks")
    if tasks_dir and os.path.isdir(tasks_dir):
        files += [os.path.join(tasks_dir, f) for f in sorted(os.listdir(tasks_dir))
                  if f.endswith((".yaml", ".yml"))]
    return files


def source_stamp(cfg: dict) -> tuple:
    return tuple((p, config.file_sig(p)) for p in source_files(cfg))


def refresh(cfg: dict) -> bool:
    """Re-read the registry if any source file changed. True = something changed.

    Called on the read paths (`get()`, GET /api/tasks) — so a new task is usable
    right away by REST, CLI, scheduler, webhook and Telegram, without a restart.
    """
    stamp = source_stamp(cfg)
    if STAMP_KEY not in cfg:
        # The first call only records the fingerprint: the registry was just built
        # from those same files (or set straight in memory) — don't clobber it.
        cfg[STAMP_KEY] = stamp
        return False
    if stamp == cfg[STAMP_KEY]:
        return False
    cfg[STAMP_KEY] = stamp
    before = set(cfg.get("tasks") or {})
    # A hand-built cfg (nloop used as a library, or a test) points at no real config
    # file — there is nothing to re-read, so its `tasks:` falls back to the boot
    # snapshot and only the tasks/ directory hot-reloads.
    files = config.source_files(cfg)
    inline = ({k: dict(v or {}) for k, v in config.section_from_disk(cfg, "tasks").items()}
              if files else cfg.get(INLINE_KEY))
    cfg["tasks"] = load_registry(cfg, inline=inline)
    after = set(cfg["tasks"])
    if before != after:
        added, gone = sorted(after - before), sorted(before - after)
        log.info("tasks reload (%s): +%s -%s", cfg.get("workspace", "?"),
                 added or "-", gone or "-")
    return True


def validate(task_id: str, spec: dict) -> None:
    if not isinstance(spec, dict):
        raise TaskError("spec must be a mapping")
    for field in ("goal", "verify_cmd"):
        if not str(spec.get(field) or "").strip():
            raise TaskError(f"field '{field}' is required")
    payload_spec = spec.get("payload") or {}
    if not isinstance(payload_spec, dict):
        raise TaskError("field 'payload' must be a mapping {required, defaults}")
    if not isinstance(payload_spec.get("required", []), list):
        raise TaskError("payload.required must be a list")
    if not isinstance(payload_spec.get("defaults", {}), dict):
        raise TaskError("payload.defaults must be a mapping")
    for field in TEMPLATED:
        if spec.get(field) is not None and not isinstance(spec[field], str):
            raise TaskError(f"field '{field}' must be a string")


def get(cfg: dict, task_id: str) -> dict:
    refresh(cfg)               # a task just written to disk is usable right away
    spec = (cfg.get("tasks") or {}).get(task_id)
    if spec is None:
        raise TaskError(f"task '{task_id}' not in the registry")
    return spec


def summary(task_id: str, spec: dict) -> dict:
    """Summary for the API/dashboard — raw spec minus the long template bodies."""
    payload_spec = spec.get("payload") or {}
    return {
        "id": task_id,
        "name": spec.get("name") or task_id,
        "description": spec.get("description") or "",
        "workdir": spec.get("workdir"),
        "verify_cmd": spec.get("verify_cmd"),
        "role": spec.get("role"),
        "has_gate": bool(spec.get("gate_prompt")),
        "required": list(payload_spec.get("required") or []),
        "defaults": dict(payload_spec.get("defaults") or {}),
        "max_iterations": spec.get("max_iterations"),
        "max_cost_usd": spec.get("max_cost_usd"),
    }


# ---- render ----

def _lookup(path: str, payload: dict):
    """`slot` and `payload.slot` both point at payload['slot']."""
    parts = path.split(".")
    cur = {"payload": payload, **payload}
    for key in parts:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def render(template: str, payload: dict) -> str:
    """Fill {{var}} from the payload. Missing variable → TaskError (fail fast)."""
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        val = _lookup(m.group(1), payload)
        if val is None:
            missing.append(m.group(1))
            return ""
        return str(val)

    out = _VAR_RE.sub(sub, template)
    if missing:
        raise TaskError(f"payload doesn't have: {', '.join(sorted(set(missing)))}")
    return out


def prepare_payload(spec: dict, payload: dict | None) -> dict:
    """Payload + defaults, checked against `payload.required`."""
    payload_spec = spec.get("payload") or {}
    merged = dict(payload_spec.get("defaults") or {})
    merged.update(payload or {})
    missing = [k for k in (payload_spec.get("required") or []) if k not in merged]
    if missing:
        raise TaskError(f"payload requires: {', '.join(missing)}")
    return merged


def resolve(cfg: dict, task_id: str, payload: dict | None = None,
            overrides: dict | None = None) -> dict:
    """Task + payload → run kwargs, ready to hand to store.create_run().

    Doesn't touch the DB — also used for preview/dry-run in the API.
    """
    spec = get(cfg, task_id)
    merged = prepare_payload(spec, payload)

    out: dict = {"task_id": task_id, "payload": merged}
    for field in TEMPLATED:
        if spec.get(field):
            out[field] = render(str(spec[field]), merged)
    for field in PASSTHROUGH:
        if spec.get(field) is not None:
            out[field] = spec[field]
    for field, value in (overrides or {}).items():
        if value is None:
            continue
        if field not in OVERRIDABLE:
            raise TaskError(f"'{field}' can't be overridden per-trigger")
        out[field] = value

    loops_cfg = cfg.get("loops", {})
    out.setdefault("model", cfg.get("claude", {}).get("model"))
    out.setdefault("max_iterations", loops_cfg.get("max_iterations", 10))
    out.setdefault("max_cost_usd", loops_cfg.get("max_cost_usd", 5.0))
    return out


# ---- trigger ----

def trigger(store, cfg: dict, task_id: str, payload: dict | None = None, *,
            idempotency_key: str | None = None,
            overrides: dict | None = None) -> dict:
    """Queue one run from a task. Returns {run_id, task, deduped, idempotency_key}.

    Idempotency key (from the argument, or the task's own `idempotency_key` when
    that's empty): as long as an ACTIVE run with the same key exists, a second
    trigger doesn't create a new run — it just points at the one already going
    (same as the webhook fingerprint dedup; it really is the same column).
    """
    spec = resolve(cfg, task_id, payload, overrides)
    key = idempotency_key or spec.pop("idempotency_key", None)
    spec.pop("idempotency_key", None)
    workspace = cfg.get("workspace")

    if key:
        # dedup inside its own workspace — a task with the same name in another
        # tenant is none of our business
        existing = store.find_active_by_fingerprint(key, workspace=workspace)
        if existing:
            return {"run_id": existing, "task": task_id, "deduped": True,
                    "idempotency_key": key}

    workdir = spec.pop("workdir", None) or _auto_workdir(cfg)
    if not os.path.isdir(workdir):
        raise TaskError(f"workdir does not exist: {workdir}")

    run_id = store.create_run(
        spec.pop("goal"), spec.pop("verify_cmd"), workdir,
        fingerprint=key, workspace=workspace, **spec,
    )
    log.info("task '%s' → run %s", task_id, run_id)
    return {"run_id": run_id, "task": task_id, "deduped": False,
            "idempotency_key": key, "workdir": workdir}


def _auto_workdir(cfg: dict) -> str:
    """Task without a workdir → a throwaway scratch dir (same as POST /api/loops)."""
    workdir = os.path.join(config.scratch_dir(cfg), uuid.uuid4().hex[:8])
    os.makedirs(workdir, exist_ok=True)
    return workdir
