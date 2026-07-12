"""Verifier deterministik: goal tercapai = perintah shell exit 0.

Sengaja TERPISAH dari agent — agent nggak boleh nilai dirinya sendiri selesai.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class VerifyResult:
    passed: bool
    exit_code: int
    output: str  # stdout+stderr digabung, di-cap dari ekor


async def verify(
    cmd: str,
    *,
    cwd: str,
    timeout_sec: int = 300,
    output_cap: int = 4000,
) -> VerifyResult:
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
        return VerifyResult(False, -1, f"[verifier timeout {timeout_sec}s]")

    text = out.decode("utf-8", "replace")
    if len(text) > output_cap:
        text = "...[dipotong]...\n" + text[-output_cap:]
    return VerifyResult(proc.returncode == 0, proc.returncode or 0, text)
