"""Adapter subprocess → `claude -p` (subscription-safe, stream-json).

Kunci kompatibilitas subscription (pola refan-agentic + dtc-agent):
- env.pop("CLAUDECODE")             → nggak kedeteksi nested session
- env.pop("ANTHROPIC_API_KEY")      → paksa auth login subscription, bukan API billing
- env.pop("ANTHROPIC_AUTH_TOKEN")   → idem (jalur token alternatif)
- env.pop("ANTHROPIC_BASE_URL")     → jangan ke-redirect ke gateway API custom
- lock_file (opsional)              → flock single-flight LINTAS proses/project
  (pola .claude.lock dtc-agent: satu subscription, jangan hammering paralel)

Output `--output-format stream-json --verbose` dibaca baris-per-baris (incremental,
jangan buffer gede) dan dipetakan ke callback event: init | turn | tool | result.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass

DEFAULT_ALLOWED_TOOLS = "Bash,Read,Edit,Write,Glob,Grep"
STREAM_LIMIT = 10 * 1024 * 1024  # baris stream-json bisa gede (tool_result)
ENV_STRIP = ("CLAUDECODE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
             "ANTHROPIC_BASE_URL")


def last_json(text: str) -> dict | None:
    """Ambil objek JSON TERAKHIR dari output model (kontrak "JSON last line" dtc:
    role diminta menutup jawaban dengan satu baris JSON, mis. {"pass": true, ...})."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


@dataclass
class ClaudeResult:
    ok: bool = False
    subtype: str = ""            # success | error_max_turns | timeout | claude_not_found | ...
    result_text: str = ""
    session_id: str | None = None
    cost_usd: float = 0.0
    num_turns: int = 0
    stderr_tail: str = ""


async def run(
    prompt: str,
    *,
    cwd: str,
    resume: str | None = None,
    session_id: str | None = None,
    model: str | None = None,
    max_turns: int | None = 30,
    allowed_tools: str = DEFAULT_ALLOWED_TOOLS,
    permission_mode: str = "acceptEdits",
    timeout_sec: int = 900,
    system_prompt: str | None = None,
    max_thinking_tokens: int | None = None,
    lock_file: str | None = None,
    on_event=None,
) -> ClaudeResult:
    """Jalankan satu iterasi `claude -p`. `on_event(type, payload)` boleh sync/async.

    - system_prompt → --append-system-prompt (role/grounding, pola dtc run_claude.sh)
    - session_id    → --session-id (mulai sesi BARU dengan id stabil yang kita pegang;
                      dipakai chat Telegram; kalau `resume` diisi, resume yang menang)
    - lock_file     → dibungkus `flock -w timeout` — single-flight lintas proses
    """
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", permission_mode,
        "--allowedTools", allowed_tools,
    ]
    if max_turns is not None:    # None = tanpa batas turn (chat Telegram)
        cmd += ["--max-turns", str(max_turns)]
    if resume:
        cmd += ["--resume", resume]
    elif session_id:
        cmd += ["--session-id", session_id]
    if model:
        cmd += ["--model", model]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    if lock_file:
        # flock nunggu maksimal se-timeout iterasi; kalau nggak kebagian, exit != 0
        # tanpa event `result` → kebaca sebagai error transient (kena retry loop).
        cmd = ["flock", "-w", str(timeout_sec), lock_file] + cmd

    env = os.environ.copy()
    for k in ENV_STRIP:
        env.pop(k, None)
    if max_thinking_tokens:
        env["MAX_THINKING_TOKENS"] = str(max_thinking_tokens)

    res = ClaudeResult()

    async def emit(type_: str, payload: dict) -> None:
        if on_event is None:
            return
        out = on_event(type_, payload)
        if asyncio.iscoroutine(out):
            await out

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
        )
    except FileNotFoundError:
        res.subtype = "claude_not_found"
        return res

    try:
        async with asyncio.timeout(timeout_sec):
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await _handle(ev, res, emit)
            await proc.wait()
            if proc.stderr is not None:
                stderr = await proc.stderr.read()
                res.stderr_tail = stderr.decode("utf-8", "replace")[-2000:]
    except TimeoutError:
        proc.kill()
        await proc.wait()
        res.ok = False
        res.subtype = "timeout"

    return res


async def _handle(ev: dict, res: ClaudeResult, emit) -> None:
    """Petakan satu baris stream-json ke ClaudeResult + event."""
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        res.session_id = ev.get("session_id")
        await emit("init", {"session_id": res.session_id, "model": ev.get("model")})
    elif t == "assistant":
        for block in (ev.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                await emit("turn", {"text": block["text"]})
            elif block.get("type") == "tool_use":
                await emit("tool", {
                    "name": block.get("name"),
                    "input": str(block.get("input"))[:200],
                })
    elif t == "result":
        res.subtype = ev.get("subtype", "")
        res.ok = res.subtype == "success"
        res.result_text = ev.get("result") or ""
        res.cost_usd = float(ev.get("total_cost_usd") or ev.get("cost_usd") or 0.0)
        res.num_turns = int(ev.get("num_turns") or 0)
        res.session_id = ev.get("session_id") or res.session_id
        await emit("result", {
            "ok": res.ok,
            "subtype": res.subtype,
            "cost_usd": res.cost_usd,
            "num_turns": res.num_turns,
        })
