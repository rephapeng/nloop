"""Deterministic verifier: the goal is met when a shell command exits 0.

Deliberately SEPARATE from the agent — the agent must never judge its own completion.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class VerifyResult:
    passed: bool
    exit_code: int
    output: str  # stdout+stderr merged, capped from the tail
    duration: float = 0.0  # seconds — used by the dashboard waterfall spans (Fase 12)


async def verify(
    cmd: str,
    *,
    cwd: str,
    timeout_sec: int = 300,
    output_cap: int = 4000,
) -> VerifyResult:
    started = time.time()
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with asyncio.timeout(timeout_sec):
            out, _ = await proc.communicate()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return VerifyResult(False, -1, f"[verifier timeout {timeout_sec}s]",
                            time.time() - started)

    text = out.decode("utf-8", "replace")
    if len(text) > output_cap:
        text = "...[truncated]...\n" + text[-output_cap:]
    return VerifyResult(proc.returncode == 0, proc.returncode or 0, text,
                        time.time() - started)
