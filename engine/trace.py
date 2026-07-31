"""Trace: runs + iterations + events → the span tree for the dashboard waterfall (Fase 12).

The old dashboard showed events as an append-only log — you could see WHAT happened,
but not HOW LONG each phase took or where the bottleneck was. This module reshapes
data that was already being written into spans with durations, trigger.dev style:

    run
    └── iteration 1
        ├── verify        (real duration from the event payload; legacy runs: estimated)
        ├── act           (real duration from the iterations table)
        │   ├── tool Bash (estimated: up to the next event — the stream has only 1 ts)
        │   └── tool Edit
        └── gate

Principle: a span whose duration is ESTIMATED is flagged `approx: true` — the frontend
renders it differently (a hatched bar) so the picture never looks more precise than it is.

Events are bucketed into iterations by the time windows from the `iterations` table
(started_at/ended_at are accurate), NOT by counting `result` events — one iteration can
emit several results when a transient retry happens.
"""
from __future__ import annotations

import time

# events that become spans of their own (the rest: turn/init/log → markers or ignored)
_ACT_STATUS = {"success": "ok"}
TOOL_CAP = 80   # tool spans per iteration; the rest collapse into one "+N more" span


def _span(span_id: str, parent: str | None, kind: str, name: str,
          start: float, end: float, status: str = "", *, approx: bool = False,
          detail: dict | None = None) -> dict:
    return {
        "id": span_id, "parent_id": parent, "kind": kind, "name": name,
        "start": start, "end": max(end, start), "duration": max(0.0, end - start),
        "status": status, "approx": approx, "detail": detail or {},
    }


def _run_status(run: dict) -> str:
    return {"succeeded": "ok", "failed": "fail", "stopped": "warn",
            "running": "running", "queued": "queued"}.get(run["status"], "")


def build(run: dict, iterations: list[dict], events: list[dict],
          now: float | None = None) -> dict:
    """Return {start, end, spans[]} — spans in time order, parents first."""
    now = now or time.time()
    t0 = run.get("started_at") or run.get("created_at") or now
    t1 = run.get("ended_at") or now
    root_name = run.get("task_id") or (run.get("goal") or "run").splitlines()[0][:60]
    spans = [_span("run", None, "run", root_name, t0, t1, _run_status(run),
                   detail={"cost": run.get("cost_total"),
                           "iterations": run.get("iterations_done")})]

    iterations = sorted(iterations, key=lambda it: it["idx"])
    windows, prev_end = [], t0
    for it in iterations:
        end = it.get("ended_at") or now
        windows.append((it, prev_end, end))
        prev_end = end

    buckets: list[list[dict]] = [[] for _ in windows]
    trailing: list[dict] = []
    for ev in sorted(events, key=lambda e: e["id"]):
        for i, (_it, _s, end) in enumerate(windows):
            if ev["ts"] <= end:
                buckets[i].append(ev)
                break
        else:
            trailing.append(ev)

    for (it, win_start, _end), evs in zip(windows, buckets):
        spans += _iteration_spans(it["idx"], it, evs, win_start, now)

    if trailing:
        if run["status"] in ("queued", "running"):
            # the in-flight iteration isn't in the iterations table yet (the row is
            # written after ACT finishes) — draw it anyway so the live view has no hole.
            spans += _iteration_spans(len(windows) + 1, None, trailing, prev_end, now)
        else:
            spans += _final_spans(trailing, prev_end, now)
    return {"start": t0, "end": max(t1, spans[-1]["end"] if spans else t1),
            "spans": spans}


def _iteration_spans(idx: int, it: dict | None, evs: list[dict],
                     win_start: float, now: float) -> list[dict]:
    parent = f"it{idx}"
    children: list[dict] = []
    prev_ts = win_start

    act_start = (it or {}).get("started_at")
    act_end = (it or {}).get("ended_at")
    tool_evs: list[dict] = []

    for ev in evs:
        typ, ts, p = ev["type"], ev["ts"], ev.get("payload") or {}
        if typ == "verify":
            children.append(_span(
                f"{parent}.verify", parent, "verify",
                "verify" + ("" if p.get("passed") else " (fail)"),
                ts - _dur(p, ts, prev_ts), ts,
                "ok" if p.get("passed") else "fail",
                approx="duration" not in p,
                detail={"exit_code": p.get("exit_code"), "output": p.get("output")}))
        elif typ == "gate":
            children.append(_span(
                f"{parent}.gate", parent, "gate", "quality gate",
                ts - _dur(p, ts, prev_ts), ts,
                "ok" if p.get("passed") else "fail",
                approx="duration" not in p,
                detail={"reasons": p.get("reasons"), "cost": p.get("cost")}))
        elif typ == "tool":
            tool_evs.append(ev)
        elif typ == "init" and act_start is None:
            act_start = ts        # iteration in flight: no table row yet
        elif typ == "result" and act_end is None:
            act_end = ts
        prev_ts = ts

    if act_start is not None:
        act_end = act_end or now
        reason = (it or {}).get("reason") or ""
        children.append(_span(
            f"{parent}.act", parent, "act", "act (claude)", act_start, act_end,
            _ACT_STATUS.get(reason, "running" if it is None else "warn"),
            detail={"cost": (it or {}).get("cost"), "turns": (it or {}).get("turns"),
                    "reason": reason,
                    "result_text": (it or {}).get("result_text")}))
        children += _tool_spans(parent, tool_evs, act_end, now)

    if not children:
        return []
    start = min(c["start"] for c in children)
    end = max(c["end"] for c in children)
    # An iteration's colour follows its ACT outcome, NOT the verify result: a failing
    # verify at the top of an iteration is the normal case (it's why the iteration runs
    # at all). A gate rejection → warn: act succeeded but the reviewer turned it down.
    if it is None:
        status = "running"
    else:
        status = "ok" if (it.get("reason") or "") == "success" else "fail"
        if any(c["kind"] == "gate" and c["status"] == "fail" for c in children):
            status = "warn"
    head = _span(parent, "run", "iteration", f"iteration {idx}", start, end, status,
                 detail={"verifier_passed": (it or {}).get("verifier_passed")})
    return [head] + children


def _tool_spans(parent: str, tool_evs: list[dict], act_end: float,
                now: float) -> list[dict]:
    """A tool event carries only ONE timestamp (when tool_use appeared in the stream).
    Its end is inferred as the next tool / the end of act → all flagged approx.

    A long iteration can hold hundreds of tool calls; drawing them all makes the
    waterfall unreadable. It is capped at TOOL_CAP and the remainder is STATED
    explicitly — a trace that truncates silently looks like a complete trace.
    """
    out = []
    shown = tool_evs[:TOOL_CAP]
    for i, ev in enumerate(shown):
        end = tool_evs[i + 1]["ts"] if i + 1 < len(tool_evs) else act_end
        p = ev.get("payload") or {}
        out.append(_span(
            f"{parent}.tool{i}", f"{parent}.act", "tool", p.get("name") or "tool",
            ev["ts"], max(end, ev["ts"]), "", approx=True,
            detail={"input": p.get("input")}))
    if len(tool_evs) > TOOL_CAP:
        rest = tool_evs[TOOL_CAP:]
        out.append(_span(
            f"{parent}.tool-rest", f"{parent}.act", "tool",
            f"+{len(rest)} more tool calls (not drawn)",
            rest[0]["ts"], act_end, "", approx=True,
            detail={"input": f"{len(rest)} tool calls hidden to keep the waterfall "
                             f"readable — the full detail is in the Log panel."}))
    return out


def _final_spans(evs: list[dict], prev_end: float, now: float) -> list[dict]:
    """Events after the last iteration: final verify, final gate, postrun (release)."""
    out, prev_ts = [], prev_end
    for i, ev in enumerate(evs):
        typ, ts, p = ev["type"], ev["ts"], ev.get("payload") or {}
        if typ == "verify":
            out.append(_span(f"final{i}", "run", "verify", "verify final",
                             ts - _dur(p, ts, prev_ts), ts,
                             "ok" if p.get("passed") else "fail",
                             approx="duration" not in p,
                             detail={"output": p.get("output")}))
        elif typ == "gate":
            out.append(_span(f"final{i}", "run", "gate", "quality gate",
                             ts - _dur(p, ts, prev_ts), ts,
                             "ok" if p.get("passed") else "fail",
                             approx="duration" not in p,
                             detail={"reasons": p.get("reasons")}))
        elif typ == "postrun":
            out.append(_span(f"final{i}", "run", "postrun", "release (on_success_cmd)",
                             ts - _dur(p, ts, prev_ts), ts,
                             "ok" if p.get("ok") else "fail",
                             approx="duration" not in p,
                             detail={"cmd": p.get("cmd"), "output": p.get("output")}))
        prev_ts = ts
    return out


def _dur(payload: dict, ts: float, prev_ts: float) -> float:
    """The real duration when recorded; legacy runs → estimate from the previous event."""
    d = payload.get("duration")
    if isinstance(d, (int, float)) and d >= 0:
        return float(d)
    return max(0.0, min(ts - prev_ts, 300.0))
