"""Loop core: observe → act → verify → gate → recover + guardrails (see PLAN.md).

The key bit: the deterministic verifier is SEPARATE from the agent — the agent
never judges itself done. Guardrails: max_iterations, max_cost_usd (+warning at
80%), stop flag between iterations, no-progress (hint → auto-stop), transient
retry, consecutive-error cap, fast-fail if the claude CLI isn't there. The
per-iteration timeout lives in claude_cli.

Ported from dtc-agent:
- role + context_cmd → system prompt (--append-system-prompt) every iteration
- gate_prompt → LLM quality gate AFTER the verifier passes (quality_gate.md
  pattern: separate reviewer, separate session, read-only, JSON last-line
  contract). Gate reject ≠ done → the loop keeps going with the reject reasons
  as feedback.
"""
from __future__ import annotations

import os
import time

from engine import claude_cli, grounding, sentry, verifier
from engine.memory import hot

NO_PROGRESS_HINT = (
    "HEADS UP: the previous iteration did NOT change the verifier output at all. "
    "CHANGE STRATEGY — do not repeat the same approach."
)

GATE_PROMPT_TEMPLATE = """You are an automated QUALITY GATE — a strict, separate
reviewer standing in for human review.
The work in this working directory has already PASSED the external verifier for this goal:
GOAL: {goal}

Judge the work against these criteria:
{criteria}

Inspect the files in the working directory yourself (read-only). Don't trust claims,
check the evidence. If in doubt, reject.

Output ONLY a single JSON object as the LAST LINE, with no other text after it:
{{"pass": true|false, "reasons": ["short reason", "..."]}}"""

# claude result subtypes worth retrying inside the same iteration.
# timeout/error_max_turns are NOT transient (those are guardrails doing their job);
# claude_not_found is fatal (a retry won't help).
_TRANSIENT_SUBTYPES = ("", "error_during_execution")


def _is_transient(res: claude_cli.ClaudeResult) -> bool:
    return not res.ok and res.subtype in _TRANSIENT_SUBTYPES


def build_prompt(goal: str, verifier_output: str, journal: str, no_progress: bool,
                 gate_reasons: list[str] | None = None) -> str:
    parts = [f"GOAL: {goal}", ""]
    if gate_reasons is not None:
        parts += [
            "External verifier PASSED, but the QUALITY GATE REJECTED the result. Reasons:",
        ] + [f"- {r}" for r in (gate_reasons or ["(no reason given)"])]
    else:
        parts += [
            "The external verifier is still FAILING. Verifier output:",
            "```",
            verifier_output.strip() or "(empty)",
            "```",
        ]
    if journal:
        parts += ["", journal]
    if no_progress:
        parts += ["", NO_PROGRESS_HINT]
    fix_target = ("Fix the work according to the gate's rejection reasons above"
                  if gate_reasons is not None
                  else "Fix what makes the verifier FAIL in this working directory")
    parts += [
        "",
        f"{fix_target}, then stop. "
        "Don't judge your own completion — the external verifier & gate decide that.",
    ]
    return "\n".join(parts)


async def _run_gate(run: dict, cfg: dict, *, workdir: str,
                    emit) -> tuple[bool, list[str], float]:
    """Run the LLM gate (session SEPARATE from the worker agent — independent reviewer).
    Returns (passed, reasons, cost). Unreadable output → counts as a reject (fail-closed),
    the iteration/budget guardrails are what bound it."""
    claude_cfg = cfg.get("claude", {})
    prompt = GATE_PROMPT_TEMPLATE.format(goal=run["goal"], criteria=run["gate_prompt"])
    res = await claude_cli.run(
        prompt,
        cwd=workdir,
        model=run["model"] or claude_cfg.get("model"),
        max_turns=claude_cfg.get("gate_max_turns", 15),
        allowed_tools=claude_cfg.get("gate_allowed_tools", "Read,Grep,Glob"),
        permission_mode="default",           # read-only, no need for acceptEdits
        timeout_sec=cfg.get("loops", {}).get("iteration_timeout_sec", 900),
        lock_file=claude_cfg.get("lock_file"),
        on_event=emit,
    )
    verdict = claude_cli.last_json(res.result_text)
    if not res.ok or verdict is None or "pass" not in verdict:
        reasons = [f"gate output unreadable (subtype={res.subtype or 'no result'})"]
        return False, reasons, res.cost_usd
    reasons = [str(r) for r in verdict.get("reasons") or []]
    return bool(verdict["pass"]), reasons, res.cost_usd


async def run_loop(run_id: str, store, cfg: dict, on_event=None) -> str:
    """Run one run until it hits a final status. Returns: succeeded|failed|stopped."""
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"run {run_id} not found")

    loops_cfg = cfg.get("loops", {})
    claude_cfg = cfg.get("claude", {})
    workdir = run["workdir"]

    def emit(type_: str, payload: dict) -> None:
        event_id = store.add_event(run_id, type_, payload)
        if on_event:  # id goes along too → SSE can dedupe replay vs live
            on_event({"id": event_id, "type": type_, "payload": payload})

    store.mark_started(run_id)
    emit("status", {"status": "running"})

    # Tier 1: seed CLAUDE.md — DON'T overwrite one the workdir already has
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
                        "output": v.output[-1000:], "duration": v.duration})
        gate_reasons: list[str] | None = None
        if v.passed:
            if not run.get("gate_prompt"):
                status, reason = "succeeded", "verifier_passed"
                break
            gate_started = time.time()
            g_pass, g_reasons, g_cost = await _run_gate(run, cfg,          # GATE
                                                        workdir=workdir, emit=emit)
            cost_total += g_cost
            store.update_cost(run_id, cost_total)
            emit("gate", {"passed": g_pass, "reasons": g_reasons, "cost": g_cost,
                          "duration": time.time() - gate_started})
            if g_pass:
                status, reason = "succeeded", "gate_passed"
                break
            if cost_total > run["max_cost_usd"]:
                status, reason = "failed", "budget_exceeded"
                break
            gate_reasons = g_reasons

        # no-progress guardrail: hint first, N times in a row → stop BEFORE
        # burning another claude iteration. Gate rejects are keyed by their reasons
        # so the same rejection twice trips this guardrail too.
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
                         "msg": f"verifier output identical {no_progress_count}x "
                                f"in a row — stop"})
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
        if claude_err_count >= max_consecutive_errors:                     # error-streak cap
            status, reason = "failed", "claude_errors"
            emit("log", {"level": "warn",
                         "msg": f"claude error {claude_err_count} iterations in a row "
                                f"(last: {res.subtype}) — stop"})
            break

        warn_at = run["max_cost_usd"] * loops_cfg.get("budget_warn_ratio", 0.8)
        if not budget_warned and cost_total >= warn_at:                    # budget alert
            budget_warned = True
            emit("log", {"level": "warn",
                         "msg": f"cost ${cost_total:.2f} is already "
                                f"{cost_total / run['max_cost_usd']:.0%} of the "
                                f"${run['max_cost_usd']:.2f} budget"})

        if cost_total > run["max_cost_usd"]:                               # budget guardrail
            status, reason = "failed", "budget_exceeded"
            break

    if status is None:
        # Iterations used up — the last action was never verified, give it a final shot
        v = await verifier.verify(run["verify_cmd"], cwd=workdir)
        emit("verify", {"passed": v.passed, "exit_code": v.exit_code,
                        "output": v.output[-1000:], "duration": v.duration})
        if v.passed and run.get("gate_prompt"):
            gate_started = time.time()
            g_pass, g_reasons, g_cost = await _run_gate(run, cfg,
                                                        workdir=workdir, emit=emit)
            cost_total += g_cost
            store.update_cost(run_id, cost_total)
            emit("gate", {"passed": g_pass, "reasons": g_reasons, "cost": g_cost,
                          "duration": time.time() - gate_started})
            status = "succeeded" if g_pass else "failed"
            reason = "gate_passed" if g_pass else "gate_rejected"
        else:
            status = "succeeded" if v.passed else "failed"
            reason = "verifier_passed" if v.passed else "max_iterations"

    # Release step (issue-fix pattern): fix verified 100% → push/deploy.
    # A failed push/deploy = run FAILED (so it gets noticed), the fix still sits in workdir.
    if status == "succeeded" and run.get("on_success_cmd"):
        p = await verifier.verify(
            run["on_success_cmd"], cwd=workdir,
            timeout_sec=loops_cfg.get("postrun_timeout_sec", 600))
        emit("postrun", {"ok": p.passed, "exit_code": p.exit_code,
                         "cmd": run["on_success_cmd"], "output": p.output[-1500:],
                         "duration": p.duration})
        if not p.passed:
            status, reason = "failed", "postrun_failed"

    # Close the cycle: mark the Sentry issue resolved (if enabled in config)
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
    """One ACT iteration + retry on transient errors. Returns (final result, total cost
    of every attempt) — a failed attempt's cost still counts (it really did get billed)."""
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
                         "msg": f"transient claude error ({res.subtype or 'no result'}), "
                                f"retry {attempt}/{retries}"})
    return res, total_cost
