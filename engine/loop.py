"""Inti loop: observe → act → verify → gate → recover + guardrails (lihat PLAN.md).

Kunci: verifier deterministik TERPISAH dari agent — agent nggak pernah menilai
dirinya sendiri selesai. Guardrails: max_iterations, max_cost_usd (+warning di
80%), stop flag antar iterasi, no-progress (hint → auto-stop), retry transient,
cap error beruntun, fast-fail kalau claude CLI nggak ada. Timeout per iterasi
di claude_cli.

Port dtc-agent:
- role + context_cmd → system prompt (--append-system-prompt) tiap iterasi
- gate_prompt → LLM quality gate SETELAH verifier lolos (pola quality_gate.md:
  reviewer terpisah, session terpisah, read-only, kontrak JSON last-line).
  Gate reject ≠ selesai → loop lanjut dengan alasan reject sebagai feedback.
"""
from __future__ import annotations

import os
import time

from engine import claude_cli, grounding, sentry, verifier
from engine.memory import hot

NO_PROGRESS_HINT = (
    "PERHATIAN: iterasi sebelumnya TIDAK mengubah hasil verifier sama sekali. "
    "GANTI STRATEGI — jangan ulangi pendekatan yang sama."
)

GATE_PROMPT_TEMPLATE = """Kamu QUALITY GATE otomatis — reviewer terpisah yang ketat (menggantikan review manusia).
Pekerjaan di working directory ini sudah LOLOS verifier eksternal untuk goal:
GOAL: {goal}

Nilai hasil kerjanya terhadap kriteria ini:
{criteria}

Periksa langsung file-file di working directory (read-only). Jangan percaya klaim,
cek buktinya. Kalau ragu, tolak.

Output HANYA satu objek JSON sebagai BARIS TERAKHIR, tanpa teks lain setelahnya:
{{"pass": true|false, "reasons": ["alasan singkat", "..."]}}"""

# subtype hasil claude yang layak dicoba ulang dalam iterasi yang sama.
# timeout/error_max_turns BUKAN transient (itu guardrail yang kerja);
# claude_not_found fatal (retry nggak bakal nolong).
_TRANSIENT_SUBTYPES = ("", "error_during_execution")


def _is_transient(res: claude_cli.ClaudeResult) -> bool:
    return not res.ok and res.subtype in _TRANSIENT_SUBTYPES


def build_prompt(goal: str, verifier_output: str, journal: str, no_progress: bool,
                 gate_reasons: list[str] | None = None) -> str:
    parts = [f"GOAL: {goal}", ""]
    if gate_reasons is not None:
        parts += [
            "Verifier eksternal LOLOS, tapi QUALITY GATE MENOLAK hasilnya. Alasan gate:",
        ] + [f"- {r}" for r in (gate_reasons or ["(tanpa alasan)"])]
    else:
        parts += [
            "Verifier eksternal masih FAIL. Output verifier:",
            "```",
            verifier_output.strip() or "(kosong)",
            "```",
        ]
    if journal:
        parts += ["", journal]
    if no_progress:
        parts += ["", NO_PROGRESS_HINT]
    fix_target = ("Perbaiki hasil kerja sesuai alasan penolakan gate di atas"
                  if gate_reasons is not None
                  else "Perbaiki penyebab FAIL di working directory ini")
    parts += [
        "",
        f"{fix_target}, lalu berhenti. "
        "Jangan menilai sendiri selesai/tidaknya — verifier & gate eksternal yang menentukan.",
    ]
    return "\n".join(parts)


async def _run_gate(run: dict, cfg: dict, *, workdir: str,
                    emit) -> tuple[bool, list[str], float]:
    """Jalankan LLM gate (session TERPISAH dari agent pekerja — reviewer independen).
    Return (passed, reasons, cost). Output tidak kebaca → dianggap reject (fail-closed),
    guardrail iterasi/budget yang membatasi."""
    claude_cfg = cfg.get("claude", {})
    prompt = GATE_PROMPT_TEMPLATE.format(goal=run["goal"], criteria=run["gate_prompt"])
    res = await claude_cli.run(
        prompt,
        cwd=workdir,
        model=run["model"] or claude_cfg.get("model"),
        max_turns=claude_cfg.get("gate_max_turns", 15),
        allowed_tools=claude_cfg.get("gate_allowed_tools", "Read,Grep,Glob"),
        permission_mode="default",           # read-only, nggak perlu acceptEdits
        timeout_sec=cfg.get("loops", {}).get("iteration_timeout_sec", 900),
        lock_file=claude_cfg.get("lock_file"),
        on_event=emit,
    )
    verdict = claude_cli.last_json(res.result_text)
    if not res.ok or verdict is None or "pass" not in verdict:
        reasons = [f"output gate tidak kebaca (subtype={res.subtype or 'no result'})"]
        return False, reasons, res.cost_usd
    reasons = [str(r) for r in verdict.get("reasons") or []]
    return bool(verdict["pass"]), reasons, res.cost_usd


async def run_loop(run_id: str, store, cfg: dict, on_event=None) -> str:
    """Jalankan satu run sampai status final. Return: succeeded|failed|stopped."""
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"run {run_id} tidak ditemukan")

    loops_cfg = cfg.get("loops", {})
    claude_cfg = cfg.get("claude", {})
    workdir = run["workdir"]

    def emit(type_: str, payload: dict) -> None:
        event_id = store.add_event(run_id, type_, payload)
        if on_event:  # id ikut dikirim → SSE bisa dedupe replay vs live
            on_event({"id": event_id, "type": type_, "payload": payload})

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
    no_progress_count = 0
    claude_err_count = 0
    budget_warned = False
    max_no_progress = loops_cfg.get("max_no_progress", 2)
    max_consecutive_errors = claude_cfg.get("max_consecutive_errors", 2)

    for idx in range(1, run["max_iterations"] + 1):
        if store.stop_requested(run_id):
            status, reason = "stopped", "stop_requested"
            break

        v = await verifier.verify(run["verify_cmd"], cwd=workdir)          # OBSERVE
        emit("verify", {"passed": v.passed, "exit_code": v.exit_code,
                        "output": v.output[-1000:]})
        gate_reasons: list[str] | None = None
        if v.passed:
            if not run.get("gate_prompt"):
                status, reason = "succeeded", "verifier_passed"
                break
            g_pass, g_reasons, g_cost = await _run_gate(run, cfg,          # GATE
                                                        workdir=workdir, emit=emit)
            cost_total += g_cost
            store.update_cost(run_id, cost_total)
            emit("gate", {"passed": g_pass, "reasons": g_reasons, "cost": g_cost})
            if g_pass:
                status, reason = "succeeded", "gate_passed"
                break
            if cost_total > run["max_cost_usd"]:
                status, reason = "failed", "budget_exceeded"
                break
            gate_reasons = g_reasons

        # guardrail no-progress: hint dulu, N kali beruntun → stop SEBELUM
        # bakar iterasi claude lagi. Gate reject dibedakan per alasan biar
        # reject dengan alasan sama 2x juga kena guardrail ini.
        effective_out = (v.output if gate_reasons is None
                         else "[gate rejected]\n" + "\n".join(gate_reasons))
        if last_out is not None and effective_out == last_out:
            no_progress_count += 1
        else:
            no_progress_count = 0
        last_out = effective_out
        if no_progress_count >= max_no_progress:
            status, reason = "failed", "no_progress"
            emit("log", {"level": "warn",
                         "msg": f"verifier output identik {no_progress_count}x beruntun — stop"})
            break

        prompt = build_prompt(run["goal"], v.output,
                              hot.journal_block(workdir), no_progress_count > 0,
                              gate_reasons=gate_reasons)
        system_prompt = await grounding.build_system_prompt(               # role + grounding
            cfg, role=run.get("role"), context_cmd=run.get("context_cmd"),
            workdir=workdir)
        started_at = time.time()
        res, iter_cost = await _act_with_retry(                            # ACT (+retry)
            prompt,
            workdir=workdir,
            session=session,
            model=run["model"] or claude_cfg.get("model"),
            claude_cfg=claude_cfg,
            timeout_sec=loops_cfg.get("iteration_timeout_sec", 900),
            system_prompt=system_prompt,
            emit=emit,
        )
        session = res.session_id or session
        cost_total += iter_cost

        store.add_iteration(
            run_id, idx=idx, prompt=prompt, result_text=res.result_text,
            cost=iter_cost, turns=res.num_turns, reason=res.subtype,
            verifier_passed=v.passed, verifier_output=effective_out[-2000:],
            started_at=started_at, ended_at=time.time(),
        )
        store.bump(run_id, cost_total=cost_total, iterations_done=idx,
                   session_id=session)
        hot.append_journal(workdir, {                                      # Tier 2
            "idx": idx,
            "action_summary": (res.result_text or res.subtype)[:200],
            "verifier_passed": v.passed,
            "error_head": effective_out[:200],
        })

        if res.subtype == "claude_not_found":                              # fatal, no retry
            status, reason = "failed", "claude_not_found"
            break

        claude_err_count = 0 if res.ok else claude_err_count + 1
        if claude_err_count >= max_consecutive_errors:                     # guardrail error beruntun
            status, reason = "failed", "claude_errors"
            emit("log", {"level": "warn",
                         "msg": f"claude error {claude_err_count} iterasi beruntun "
                                f"(terakhir: {res.subtype}) — stop"})
            break

        warn_at = run["max_cost_usd"] * loops_cfg.get("budget_warn_ratio", 0.8)
        if not budget_warned and cost_total >= warn_at:                    # budget alert
            budget_warned = True
            emit("log", {"level": "warn",
                         "msg": f"cost ${cost_total:.2f} udah "
                                f"{cost_total / run['max_cost_usd']:.0%} dari budget "
                                f"${run['max_cost_usd']:.2f}"})

        if cost_total > run["max_cost_usd"]:                               # guardrail budget
            status, reason = "failed", "budget_exceeded"
            break

    if status is None:
        # Iterasi habis — aksi terakhir belum sempet diverifikasi, kasih kesempatan final
        v = await verifier.verify(run["verify_cmd"], cwd=workdir)
        emit("verify", {"passed": v.passed, "exit_code": v.exit_code,
                        "output": v.output[-1000:]})
        if v.passed and run.get("gate_prompt"):
            g_pass, g_reasons, g_cost = await _run_gate(run, cfg,
                                                        workdir=workdir, emit=emit)
            cost_total += g_cost
            store.update_cost(run_id, cost_total)
            emit("gate", {"passed": g_pass, "reasons": g_reasons, "cost": g_cost})
            status = "succeeded" if g_pass else "failed"
            reason = "gate_passed" if g_pass else "gate_rejected"
        else:
            status = "succeeded" if v.passed else "failed"
            reason = "verifier_passed" if v.passed else "max_iterations"

    # Langkah rilis (pola issue-fix): fix terverifikasi 100% → push/deploy.
    # Gagal push/deploy = run FAILED (biar ke-notif), fix-nya tetap ada di workdir.
    if status == "succeeded" and run.get("on_success_cmd"):
        p = await verifier.verify(
            run["on_success_cmd"], cwd=workdir,
            timeout_sec=loops_cfg.get("postrun_timeout_sec", 600))
        emit("postrun", {"ok": p.passed, "exit_code": p.exit_code,
                         "cmd": run["on_success_cmd"], "output": p.output[-1500:]})
        if not p.passed:
            status, reason = "failed", "postrun_failed"

    # Tutup siklus: issue Sentry di-mark resolved (kalau diaktifkan di config)
    if status == "succeeded":
        note = await sentry.resolve_issue(run.get("fingerprint"), cfg)
        if note:
            emit("log", {"level": note[0], "msg": note[1]})

    store.finish(run_id, status)
    emit("status", {"status": status, "reason": reason, "cost_total": cost_total})
    return status


async def _act_with_retry(prompt: str, *, workdir: str, session: str | None,
                          model: str | None, claude_cfg: dict,
                          timeout_sec: int, emit,
                          system_prompt: str | None = None,
                          ) -> tuple[claude_cli.ClaudeResult, float]:
    """Satu iterasi ACT + retry untuk error transient. Return (hasil akhir, total cost
    semua attempt) — cost attempt yang gagal tetap dihitung (kejadian beneran kebayar)."""
    retries = claude_cfg.get("retries", 1)
    total_cost = 0.0
    res = claude_cli.ClaudeResult()
    for attempt in range(1, retries + 2):
        res = await claude_cli.run(
            prompt,
            cwd=workdir,
            resume=session,
            model=model,
            max_turns=claude_cfg.get("max_turns", 30),
            allowed_tools=claude_cfg.get("allowed_tools", claude_cli.DEFAULT_ALLOWED_TOOLS),
            permission_mode=claude_cfg.get("permission_mode", "acceptEdits"),
            timeout_sec=timeout_sec,
            system_prompt=system_prompt,
            lock_file=claude_cfg.get("lock_file"),
            on_event=emit,
        )
        total_cost += res.cost_usd
        session = res.session_id or session
        if res.subtype == "claude_not_found" or not _is_transient(res):
            break
        if attempt <= retries:
            emit("log", {"level": "warn",
                         "msg": f"claude error transient ({res.subtype or 'no result'}), "
                                f"retry {attempt}/{retries}"})
    return res, total_cost
