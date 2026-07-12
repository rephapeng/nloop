"""Inti loop: observe → act → verify → recover + guardrails.  TODO(Fase 2)

Pseudocode (lihat PLAN.md):
    loop(run):
      session = None
      for i in 1..max_iterations:
        if stop_requested(run): -> status=stopped; break
        passed, out = verify(run.verify_cmd)        # OBSERVE
        if passed: -> status=succeeded; break
        no_progress = (out == last_out and i>1)
        prompt = build_prompt(goal, out, no_progress)
        res = claude_cli.run(prompt, resume=session, on_event=emit)   # ACT
        ...guardrails: budget, timeout, max_turns...
"""
from __future__ import annotations


async def run_loop(run: dict, store, bus, cfg: dict) -> None:
    raise NotImplementedError("TODO(Fase 2): loop core")
