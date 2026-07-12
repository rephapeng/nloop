"""Inti loop: observe → act → verify → recover + guardrails (lihat PLAN.md).

Kunci: verifier deterministik TERPISAH dari agent — agent nggak pernah menilai
dirinya sendiri selesai. Guardrails di sini: max_iterations, max_cost_usd,
stop flag (dicek antar iterasi), no-progress hint. Timeout per iterasi ada di
claude_cli. Sisanya (retry transient, dsb.) nyusul Fase 6.
"""
from __future__ import annotations

import os
import time

from engine import claude_cli, verifier
from engine.memory import hot

NO_PROGRESS_HINT = (
    "PERHATIAN: iterasi sebelumnya TIDAK mengubah hasil verifier sama sekali. "
    "GANTI STRATEGI — jangan ulangi pendekatan yang sama."
)


def build_prompt(goal: str, verifier_output: str, journal: str, no_progress: bool) -> str:
    parts = [
        f"GOAL: {goal}",
        "",
        "Verifier eksternal masih FAIL. Output verifier:",
        "```",
        verifier_output.strip() or "(kosong)",
        "```",
    ]
    if journal:
        parts += ["", journal]
    if no_progress:
        parts += ["", NO_PROGRESS_HINT]
    parts += [
        "",
        "Perbaiki penyebab FAIL di working directory ini, lalu berhenti. "
        "Jangan menilai sendiri selesai/tidaknya — verifier eksternal yang menentukan.",
    ]
    return "\n".join(parts)


async def run_loop(run_id: str, store, cfg: dict, on_event=None) -> str:
    """Jalankan satu run sampai status final. Return: succeeded|failed|stopped."""
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"run {run_id} tidak ditemukan")

    loops_cfg = cfg.get("loops", {})
    claude_cfg = cfg.get("claude", {})
    workdir = run["workdir"]

    def emit(type_: str, payload: dict) -> None:
        store.add_event(run_id, type_, payload)
        if on_event:
            on_event(type_, payload)

    store.mark_started(run_id)
    emit("status", {"status": "running"})

    # Tier 1: seed CLAUDE.md — JANGAN timpa kalau workdir udah punya
    if not os.path.exists(os.path.join(workdir, "CLAUDE.md")):
        hot.seed_claudemd(workdir, run["goal"])

    session: str | None = run["session_id"]
    cost_total: float = run["cost_total"] or 0.0
    last_out: str | None = None
    status: str | None = None
    reason = ""

    for idx in range(1, run["max_iterations"] + 1):
        if store.stop_requested(run_id):
            status, reason = "stopped", "stop_requested"
            break

        v = await verifier.verify(run["verify_cmd"], cwd=workdir)          # OBSERVE
        emit("verify", {"passed": v.passed, "exit_code": v.exit_code,
                        "output": v.output[-1000:]})
        if v.passed:
            status, reason = "succeeded", "verifier_passed"
            break

        no_progress = last_out is not None and v.output == last_out
        last_out = v.output

        prompt = build_prompt(run["goal"], v.output,
                              hot.journal_block(workdir), no_progress)
        started_at = time.time()
        res = await claude_cli.run(                                        # ACT
            prompt,
            cwd=workdir,
            resume=session,
            model=run["model"] or claude_cfg.get("model"),
            max_turns=claude_cfg.get("max_turns", 30),
            allowed_tools=claude_cfg.get("allowed_tools", claude_cli.DEFAULT_ALLOWED_TOOLS),
            permission_mode=claude_cfg.get("permission_mode", "acceptEdits"),
            timeout_sec=loops_cfg.get("iteration_timeout_sec", 900),
            on_event=emit,
        )
        session = res.session_id or session
        cost_total += res.cost_usd

        store.add_iteration(
            run_id, idx=idx, prompt=prompt, result_text=res.result_text,
            cost=res.cost_usd, turns=res.num_turns, reason=res.subtype,
            verifier_passed=False, verifier_output=v.output[-2000:],
            started_at=started_at, ended_at=time.time(),
        )
        store.bump(run_id, cost_total=cost_total, iterations_done=idx,
                   session_id=session)
        hot.append_journal(workdir, {                                      # Tier 2
            "idx": idx,
            "action_summary": (res.result_text or res.subtype)[:200],
            "verifier_passed": False,
            "error_head": v.output[:200],
        })

        if cost_total > run["max_cost_usd"]:                               # guardrail budget
            status, reason = "failed", "budget_exceeded"
            break

    if status is None:
        # Iterasi habis — aksi terakhir belum sempet diverifikasi, kasih kesempatan final
        v = await verifier.verify(run["verify_cmd"], cwd=workdir)
        emit("verify", {"passed": v.passed, "exit_code": v.exit_code,
                        "output": v.output[-1000:]})
        status = "succeeded" if v.passed else "failed"
        reason = "verifier_passed" if v.passed else "max_iterations"

    store.finish(run_id, status)
    emit("status", {"status": status, "reason": reason, "cost_total": cost_total})
    return status
