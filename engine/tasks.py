"""Task registry: definisi task reusable + trigger berpayload (pola trigger.dev).

Sebelum ini tiap entry path (REST, scheduler, webhook, Telegram) nyusun run
sendiri-sendiri — tiap run jadi barang sekali pakai, nggak ada "task" yang bisa
dilihat/dijalankan ulang. Sekarang semuanya lewat SATU pintu:

    tasks.trigger(store, cfg, "<task_id>", payload)

Task didefinisiin sekali di `tasks:` config.yaml ATAU file `tasks/<id>.yaml`
(satu file = satu task, id = nama file). Field yang bisa di-template ({{...}})
diisi dari payload waktu trigger:

    tasks:
      promo-post:
        name: "Promo post MarginIn"
        payload:
          required: [slot]                # nggak ada → TaskError (fail cepat)
          defaults: {n_recent: 10}
        goal: "Bikin gimmick post slot {{slot}} ..."
        verify_cmd: "scripts/buffer_post.py verify --slot {{slot}}"
        workdir: /opt/nloop
        idempotency_key: "promo:{{slot}}"  # dedup: 1 run aktif per slot

`{{slot}}` dan `{{payload.slot}}` dua-duanya jalan. Variabel yang nggak ada di
payload = error waktu trigger (bukan string kosong diam-diam) — goal yang bolong
bikin agent ngarang.
"""
from __future__ import annotations

import logging
import os
import re
import uuid

import yaml

log = logging.getLogger("nloop.tasks")

# Field task yang isinya di-render dari payload.
TEMPLATED = ("goal", "verify_cmd", "workdir", "context_cmd", "gate_prompt",
             "on_success_cmd", "idempotency_key")
# Field non-template yang diturunkan apa adanya ke run.
PASSTHROUGH = ("model", "max_iterations", "max_cost_usd", "role")
# Override yang boleh dikirim per-trigger (payload nggak boleh ganti workdir dst.)
OVERRIDABLE = ("model", "max_iterations", "max_cost_usd", "workdir")

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


class TaskError(ValueError):
    """Task nggak ada / spec rusak / payload nggak lengkap."""


# ---- registry ----

def load_registry(cfg: dict) -> dict[str, dict]:
    """Gabung `tasks:` config.yaml + file `<paths.tasks>/<id>.yaml`.

    File di direktori menang atas config.yaml (pola "task as code": definisinya
    ikut repo, bukan numpuk di satu config raksasa). Spec rusak di-LOG dan
    di-skip — satu task salah ketik nggak boleh matiin server.
    """
    registry: dict[str, dict] = {}
    for task_id, spec in (cfg.get("tasks") or {}).items():
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
                log.error("task file %s nggak kebaca, di-skip: %s", path, e)
                continue
            if not isinstance(spec, dict):
                log.error("task file %s bukan mapping, di-skip", path)
                continue
            registry[os.path.splitext(fname)[0]] = spec

    valid = {}
    for task_id, spec in registry.items():
        try:
            validate(task_id, spec)
        except TaskError as e:
            log.error("task '%s' invalid, di-skip: %s", task_id, e)
            continue
        valid[task_id] = spec
    if valid:
        log.info("tasks: %d task kedaftar", len(valid))
    return valid


def validate(task_id: str, spec: dict) -> None:
    if not isinstance(spec, dict):
        raise TaskError("spec harus mapping")
    for field in ("goal", "verify_cmd"):
        if not str(spec.get(field) or "").strip():
            raise TaskError(f"field '{field}' wajib diisi")
    payload_spec = spec.get("payload") or {}
    if not isinstance(payload_spec, dict):
        raise TaskError("field 'payload' harus mapping {required, defaults}")
    if not isinstance(payload_spec.get("required", []), list):
        raise TaskError("payload.required harus list")
    if not isinstance(payload_spec.get("defaults", {}), dict):
        raise TaskError("payload.defaults harus mapping")
    for field in TEMPLATED:
        if spec.get(field) is not None and not isinstance(spec[field], str):
            raise TaskError(f"field '{field}' harus string")


def get(cfg: dict, task_id: str) -> dict:
    spec = (cfg.get("tasks") or {}).get(task_id)
    if spec is None:
        raise TaskError(f"task '{task_id}' nggak ada di registry")
    return spec


def summary(task_id: str, spec: dict) -> dict:
    """Ringkasan buat API/dashboard — spec mentah tanpa isi template panjang."""
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
    """`slot` dan `payload.slot` sama-sama nunjuk payload['slot']."""
    parts = path.split(".")
    cur = {"payload": payload, **payload}
    for key in parts:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def render(template: str, payload: dict) -> str:
    """Isi {{var}} dari payload. Variabel nggak ada → TaskError (fail cepat)."""
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        val = _lookup(m.group(1), payload)
        if val is None:
            missing.append(m.group(1))
            return ""
        return str(val)

    out = _VAR_RE.sub(sub, template)
    if missing:
        raise TaskError(f"payload nggak punya: {', '.join(sorted(set(missing)))}")
    return out


def prepare_payload(spec: dict, payload: dict | None) -> dict:
    """Payload + defaults, dicek terhadap `payload.required`."""
    payload_spec = spec.get("payload") or {}
    merged = dict(payload_spec.get("defaults") or {})
    merged.update(payload or {})
    missing = [k for k in (payload_spec.get("required") or []) if k not in merged]
    if missing:
        raise TaskError(f"payload wajib: {', '.join(missing)}")
    return merged


def resolve(cfg: dict, task_id: str, payload: dict | None = None,
            overrides: dict | None = None) -> dict:
    """Task + payload → kwargs run yang siap dikirim ke store.create_run().

    Nggak nyentuh DB — dipakai juga buat preview/dry-run di API.
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
            raise TaskError(f"'{field}' nggak boleh di-override per-trigger")
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
    """Antri satu run dari task. Return {run_id, task, deduped, idempotency_key}.

    Idempotency key (dari argumen, kalau kosong dari `idempotency_key` task):
    selama masih ada run AKTIF dengan key yang sama, trigger kedua nggak bikin
    run baru — cuma nunjuk yang lagi jalan (sama kayak dedup fingerprint webhook,
    memang kolom yang sama).
    """
    spec = resolve(cfg, task_id, payload, overrides)
    key = idempotency_key or spec.pop("idempotency_key", None)
    spec.pop("idempotency_key", None)

    if key:
        existing = store.find_active_by_fingerprint(key)
        if existing:
            return {"run_id": existing, "task": task_id, "deduped": True,
                    "idempotency_key": key}

    workdir = spec.pop("workdir", None) or _auto_workdir(cfg)
    if not os.path.isdir(workdir):
        raise TaskError(f"workdir nggak ada: {workdir}")

    run_id = store.create_run(
        spec.pop("goal"), spec.pop("verify_cmd"), workdir,
        fingerprint=key, **spec,
    )
    log.info("task '%s' → run %s", task_id, run_id)
    return {"run_id": run_id, "task": task_id, "deduped": False,
            "idempotency_key": key, "workdir": workdir}


def _auto_workdir(cfg: dict) -> str:
    """Task tanpa workdir → workspace sekali pakai (sama kayak POST /api/loops)."""
    workdir = os.path.join(cfg.get("paths", {}).get("workspaces", "workspaces"),
                           uuid.uuid4().hex[:8])
    os.makedirs(workdir, exist_ok=True)
    return workdir
