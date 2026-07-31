"""Trace builder: events + iterations → span waterfall (Fase 12)."""
import pytest

from engine import trace

T0 = 1_000_000.0


def run(status="succeeded", **kw):
    base = {"id": "r1", "status": status, "goal": "build something", "task_id": None,
            "created_at": T0, "started_at": T0, "ended_at": T0 + 100,
            "cost_total": 0.5, "iterations_done": 1}
    base.update(kw)
    return base


def it(idx=1, start=T0 + 5, end=T0 + 60, **kw):
    base = {"idx": idx, "started_at": start, "ended_at": end, "cost": 0.2,
            "turns": 3, "reason": "success", "verifier_passed": 0,
            "result_text": "done"}
    base.update(kw)
    return base


def ev(eid, ts, type_, **payload):
    return {"id": eid, "ts": ts, "type": type_, "payload": payload}


def by_id(spans):
    return {s["id"]: s for s in spans}


# ---- basic structure ----

def test_empty_run_has_only_root():
    t = trace.build(run(status="queued", started_at=None, ended_at=None), [], [],
                    now=T0 + 3)
    assert [s["kind"] for s in t["spans"]] == ["run"]
    assert t["spans"][0]["status"] == "queued"


def test_one_full_iteration():
    events = [
        ev(1, T0 + 1, "status", status="running"),
        ev(2, T0 + 4, "verify", passed=False, exit_code=1, output="boom", duration=2.5),
        ev(3, T0 + 6, "init", session_id="s"),
        ev(4, T0 + 20, "tool", name="Bash", input="pytest"),
        ev(5, T0 + 40, "tool", name="Edit", input="app.py"),
        ev(6, T0 + 60, "result", subtype="success", num_turns=3),
        ev(7, T0 + 62, "verify", passed=True, exit_code=0, output="ok", duration=1.0),
        ev(8, T0 + 63, "status", status="succeeded"),
    ]
    spans = by_id(trace.build(run(), [it()], events)["spans"])

    assert spans["it1"]["kind"] == "iteration"
    assert spans["it1.verify"]["duration"] == pytest.approx(2.5)   # measured duration
    assert spans["it1.verify"]["approx"] is False
    assert spans["it1.verify"]["status"] == "fail"
    assert spans["it1.act"]["duration"] == pytest.approx(55)       # from iterations table
    assert spans["it1.act"]["parent_id"] == "it1"
    assert spans["it1.tool0"]["name"] == "Bash"
    assert spans["it1.tool0"]["parent_id"] == "it1.act"
    assert spans["it1.tool0"]["approx"] is True                    # end is inferred
    assert spans["it1.tool0"]["end"] == T0 + 40                    # up to the next tool
    assert spans["it1.tool1"]["end"] == T0 + 60                    # last tool → end of act
    assert spans["final0"]["kind"] == "verify"                     # final verify at root
    assert spans["final0"]["parent_id"] == "run"


def test_verify_without_duration_is_estimated():
    """Old runs (pre Fase 12) don't store the verify duration → approx, not zero."""
    events = [ev(1, T0 + 10, "verify", passed=True, exit_code=0, output="ok")]
    spans = by_id(trace.build(run(), [it()], events)["spans"])
    assert spans["it1.verify"]["approx"] is True
    assert spans["it1.verify"]["duration"] == pytest.approx(10)    # from window start


def test_second_iteration_does_not_steal_first_iteration_events():
    events = [
        ev(1, T0 + 4, "verify", passed=False, duration=1),
        ev(2, T0 + 60, "result", subtype="success"),
        ev(3, T0 + 64, "verify", passed=False, duration=1),
        ev(4, T0 + 90, "result", subtype="success"),
    ]
    spans = by_id(trace.build(run(), [it(1), it(2, T0 + 65, T0 + 90)], events)["spans"])
    assert spans["it1.verify"]["end"] == T0 + 4
    assert spans["it2.verify"]["end"] == T0 + 64
    assert spans["it2.act"]["duration"] == pytest.approx(25)


def test_retry_inside_an_iteration_still_one_act_span():
    """Two result events inside one iteration window (transient retry) — act stays 1."""
    events = [
        ev(1, T0 + 4, "verify", passed=False, duration=1),
        ev(2, T0 + 10, "result", subtype="error_during_execution"),
        ev(3, T0 + 12, "log", level="warn", msg="retry 1/1"),
        ev(4, T0 + 55, "result", subtype="success"),
    ]
    spans = trace.build(run(), [it()], events)["spans"]
    assert len([s for s in spans if s["kind"] == "act"]) == 1


def test_gate_gets_its_own_span():
    events = [
        ev(1, T0 + 4, "verify", passed=True, duration=1),
        ev(2, T0 + 30, "gate", passed=False, reasons=["too short"], cost=0.1,
           duration=8.0),
        ev(3, T0 + 60, "result", subtype="success"),
    ]
    spans = by_id(trace.build(run(), [it()], events)["spans"])
    assert spans["it1.gate"]["duration"] == pytest.approx(8.0)
    assert spans["it1.gate"]["status"] == "fail"
    assert spans["it1.gate"]["detail"]["reasons"] == ["too short"]
    # act succeeded but the gate rejected the work → warn (not fail, not ok)
    assert spans["it1"]["status"] == "warn"


def test_postrun_becomes_a_final_span():
    events = [
        ev(1, T0 + 4, "verify", passed=False, duration=1),
        ev(2, T0 + 60, "result", subtype="success"),
        ev(3, T0 + 62, "verify", passed=True, duration=1),
        ev(4, T0 + 80, "postrun", ok=True, cmd="git push", output="", duration=15.0),
    ]
    spans = [s for s in trace.build(run(), [it()], events)["spans"]
             if s["kind"] == "postrun"]
    assert len(spans) == 1
    assert spans[0]["duration"] == pytest.approx(15.0)
    assert spans[0]["detail"]["cmd"] == "git push"


def test_running_iteration_is_drawn_even_before_it_hits_the_table():
    """The iterations row is only written after ACT finishes — the live view must
    not have a hole in it."""
    events = [
        ev(1, T0 + 4, "verify", passed=False, duration=1),
        ev(2, T0 + 6, "init", session_id="s"),
        ev(3, T0 + 20, "tool", name="Bash", input="pytest"),
    ]
    t = trace.build(run(status="running", ended_at=None), [], events, now=T0 + 30)
    spans = by_id(t["spans"])
    assert spans["it1"]["status"] == "running"
    assert spans["it1.act"]["end"] == T0 + 30      # still running → up to now
    assert spans["it1.tool0"]["end"] == T0 + 30


def test_every_span_has_a_parent_that_exists():
    events = [
        ev(1, T0 + 4, "verify", passed=False, duration=1),
        ev(2, T0 + 6, "init"),
        ev(3, T0 + 20, "tool", name="Bash"),
        ev(4, T0 + 60, "result", subtype="success"),
        ev(5, T0 + 62, "verify", passed=True, duration=1),
    ]
    spans = trace.build(run(), [it()], events)["spans"]
    ids = {s["id"] for s in spans}
    assert all(s["parent_id"] in ids for s in spans if s["parent_id"])
    assert all(s["end"] >= s["start"] for s in spans)


def test_failing_verify_at_top_of_iteration_is_not_a_failed_iteration():
    """A FAIL verify at the top is normal — it's the very reason the iteration
    runs. What decides the iteration's color is the outcome of its ACT."""
    events = [
        ev(1, T0 + 4, "verify", passed=False, duration=1),
        ev(2, T0 + 60, "result", subtype="success"),
    ]
    spans = by_id(trace.build(run(), [it()], events)["spans"])
    assert spans["it1.verify"]["status"] == "fail"
    assert spans["it1"]["status"] == "ok"


def test_act_error_makes_the_iteration_fail():
    events = [
        ev(1, T0 + 4, "verify", passed=False, duration=1),
        ev(2, T0 + 60, "result", subtype="error_max_turns"),
    ]
    spans = by_id(trace.build(run(), [it(reason="error_max_turns")], events)["spans"])
    assert spans["it1"]["status"] == "fail"


def test_tools_are_capped_but_the_cut_is_stated():
    """An iteration with hundreds of tools: cut at TOOL_CAP, the rest summarized —
    not silently gone."""
    n = trace.TOOL_CAP + 25
    events = [ev(1, T0 + 4, "verify", passed=False, duration=1)]
    events += [ev(2 + i, T0 + 5 + i * 0.1, "tool", name=f"T{i}") for i in range(n)]
    events.append(ev(500, T0 + 60, "result", subtype="success"))

    spans = trace.build(run(), [it()], events)["spans"]
    tools = [s for s in spans if s["kind"] == "tool"]
    assert len(tools) == trace.TOOL_CAP + 1          # + one summary span
    assert "25 more tool calls" in tools[-1]["name"]
