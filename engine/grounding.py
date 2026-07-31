"""Roles + grounding → system prompt (the dtc-agent run_claude.sh pattern).

The system prompt is assembled from three layers, all optional:
1. roles/common.md            — shared rules, always prepended when present
2. `context_cmd` output       — FRESH grounding: a shell command run in the
   workdir every iteration, its stdout injected (dtc's build_knowledge.py
   pattern: the agent may only mention things that actually exist,
   "don't leave context")
3. roles/<role>.md            — role-specific fragment (writer, reviewer, ...)

The result is passed as `--append-system-prompt` — unlike CLAUDE.md (Tier 1 hot
memory): CLAUDE.md is curated by the engine per run, role/grounding is static
per config.
"""
from __future__ import annotations

import asyncio
import os

CONTEXT_CAP = 24_000  # chars — a huge grounding makes every iteration expensive
CONTEXT_TIMEOUT_SEC = 60


def role_prompt(cfg: dict, role: str) -> str:
    """Read roles/<role>.md. A missing role means a typo → fail fast and loudly."""
    path = os.path.join(cfg["paths"].get("roles", "roles"), f"{role}.md")
    if not os.path.isfile(path):
        raise ValueError(f"role '{role}' does not exist ({path})")
    with open(path) as f:
        return f.read().strip()


def common_prompt(cfg: dict) -> str:
    path = os.path.join(cfg["paths"].get("roles", "roles"), "common.md")
    if not os.path.isfile(path):
        return ""
    with open(path) as f:
        return f.read().strip()


async def run_context_cmd(cmd: str, *, cwd: str) -> str:
    """Run context_cmd, return its stdout (capped). Failure → a warning, not fatal:
    grounding is an aid, the loop still has to work without it."""
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
        return f"[context_cmd failed to run: {e}]"

    text = out.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        return f"[context_cmd exit {proc.returncode}]\n{text[-2000:]}"
    if len(text) > CONTEXT_CAP:
        text = text[:CONTEXT_CAP] + "\n[... grounding truncated at the cap]"
    return text


async def build_system_prompt(
    cfg: dict, *, role: str | None = None, context_cmd: str | None = None,
    workdir: str = ".",
) -> str | None:
    """Assemble the system prompt: common + grounding + role. All empty → None."""
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
