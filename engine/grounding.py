"""Roles + grounding → system prompt (pola dtc-agent run_claude.sh).

System prompt dirakit dari tiga lapis, semuanya opsional:
1. roles/common.md            — aturan bersama, selalu di-prepend kalau ada
2. output `context_cmd`       — grounding SEGAR: perintah shell dijalankan di
   workdir tiap iterasi, stdout-nya di-inject (pola build_knowledge.py dtc:
   agent cuma boleh nyebut hal yang beneran ada, "don't leave context")
3. roles/<role>.md            — fragment role spesifik (writer, reviewer, dst.)

Hasil dipakai sebagai `--append-system-prompt` — beda dengan CLAUDE.md (Tier 1
hot memory): CLAUDE.md dikurasi engine per-run, role/grounding statis per-config.
"""
from __future__ import annotations

import asyncio
import os

CONTEXT_CAP = 24_000  # chars — grounding gede bikin tiap iterasi mahal
CONTEXT_TIMEOUT_SEC = 60


def role_prompt(cfg: dict, role: str) -> str:
    """Baca roles/<role>.md. Role nggak ada = salah ketik → fail cepat & jelas."""
    path = os.path.join(cfg["paths"].get("roles", "roles"), f"{role}.md")
    if not os.path.isfile(path):
        raise ValueError(f"role '{role}' tidak ada ({path})")
    with open(path) as f:
        return f.read().strip()


def common_prompt(cfg: dict) -> str:
    path = os.path.join(cfg["paths"].get("roles", "roles"), "common.md")
    if not os.path.isfile(path):
        return ""
    with open(path) as f:
        return f.read().strip()


async def run_context_cmd(cmd: str, *, cwd: str) -> str:
    """Jalankan context_cmd, balikin stdout (di-cap). Gagal → warning, bukan fatal:
    grounding itu bantuan, loop tetap harus jalan tanpa dia."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async with asyncio.timeout(CONTEXT_TIMEOUT_SEC):
            out, _ = await proc.communicate()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return f"[context_cmd timeout {CONTEXT_TIMEOUT_SEC}s: {cmd}]"
    except OSError as e:
        return f"[context_cmd gagal jalan: {e}]"

    text = out.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        return f"[context_cmd exit {proc.returncode}]\n{text[-2000:]}"
    if len(text) > CONTEXT_CAP:
        text = text[:CONTEXT_CAP] + "\n[... grounding dipotong di cap]"
    return text


async def build_system_prompt(
    cfg: dict, *, role: str | None = None, context_cmd: str | None = None,
    workdir: str = ".",
) -> str | None:
    """Rakit system prompt: common + grounding + role. Kosong semua → None."""
    parts: list[str] = []
    common = common_prompt(cfg)
    if common:
        parts.append(common)
    if context_cmd:
        grounding = await run_context_cmd(context_cmd, cwd=workdir)
        if grounding:
            parts.append("===== INJECTED GROUNDING (context_cmd) =====\n" + grounding)
    if role:
        parts.append("===== ROLE =====\n" + role_prompt(cfg, role))
    return "\n\n".join(parts) if parts else None
