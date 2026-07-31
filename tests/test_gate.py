"""LLM quality gate (port of dtc's quality_gate): verifier passes → gate judges it.

The fake claude tells gate calls from act calls by the "QUALITY GATE" marker in
the prompt.
"""
import asyncio
import json
from pathlib import Path

import pytest

from engine import config, loop
from engine.claude_cli import ClaudeResult
from engine.store import Store


@pytest.fixture
def cfg():
    c = config.load("/nonexistent")
    c["claude"]["model"] = "fake"
    return c


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


@pytest.fixture
def workdir(tmp_path):
    wd = tmp_path / "ws"
    wd.mkdir()
    return str(wd)


def make_fake(monkeypatch, verdicts, fixer=None, act_cost=0.01, gate_cost=0.02):
    """verdicts: queue of gate replies (dict → JSON last line, str → sent raw)."""
    acts, gates = [], []

    async def fake_run(prompt, *, cwd, resume=None, **kwargs):
        # An ACT prompt can also mention "QUALITY GATE REJECTED" (feedback), so
        # detect gate calls by the template's opening sentence.
        if prompt.startswith("You are an automated QUALITY GATE"):
            gates.append(prompt)
            v = verdicts.pop(0)
            text = "review notes...\n" + (v if isinstance(v, str) else json.dumps(v))
            return ClaudeResult(ok=True, subtype="success", result_text=text,
                                cost_usd=gate_cost, num_turns=1)
        acts.append(prompt)
        if fixer:
            fixer(cwd)
        return ClaudeResult(ok=True, subtype="success",
                            result_text=f"action-{len(acts)}",
                            session_id=f"s{len(acts)}", cost_usd=act_cost, num_turns=2)

    monkeypatch.setattr(loop.claude_cli, "run", fake_run)
    return acts, gates


def run(store, cfg, run_id):
    return asyncio.run(loop.run_loop(run_id, store, cfg))


def test_gate_fields_roundtrip(store, workdir):
    run_id = store.create_run("g", "exit 0", workdir, role="writer",
                              context_cmd="echo ctx", gate_prompt="must be good")
    r = store.get_run(run_id)
    assert (r["role"], r["context_cmd"], r["gate_prompt"]) == \
        ("writer", "echo ctx", "must be good")


def test_gate_pass_first_try(monkeypatch, store, cfg, workdir):
    acts, gates = make_fake(monkeypatch, [{"pass": True, "reasons": []}])
    run_id = store.create_run("g", "exit 0", workdir, gate_prompt="criterion X")
    assert run(store, cfg, run_id) == "succeeded"
    assert acts == [] and len(gates) == 1
    assert "criterion X" in gates[0]

    r = store.get_run(run_id)
    assert r["cost_total"] == pytest.approx(0.02)     # gate cost is recorded
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "gate_passed"


def test_gate_reject_feeds_reasons_to_next_iteration(monkeypatch, store, cfg, workdir):
    acts, gates = make_fake(monkeypatch, [
        {"pass": False, "reasons": ["too shallow", "no examples"]},
        {"pass": True},
    ])
    run_id = store.create_run("g", "exit 0", workdir, gate_prompt="criteria")
    assert run(store, cfg, run_id) == "succeeded"
    assert len(gates) == 2 and len(acts) == 1
    assert "QUALITY GATE REJECTED" in acts[0]
    assert "too shallow" in acts[0]

    its = store.iterations(run_id)
    assert len(its) == 1 and its[0]["verifier_passed"] == 1   # verifier passed, gate rejected
    assert "[gate rejected]" in its[0]["verifier_output"]


def test_gate_without_gate_prompt_untouched(monkeypatch, store, cfg, workdir):
    acts, gates = make_fake(monkeypatch, [])
    run_id = store.create_run("g", "exit 0", workdir)
    assert run(store, cfg, run_id) == "succeeded"
    assert gates == []                                # gate is never called


def test_gate_unparseable_output_is_reject(monkeypatch, store, cfg, workdir):
    acts, gates = make_fake(monkeypatch, ["not json at all", {"pass": True}])
    run_id = store.create_run("g", "exit 0", workdir, gate_prompt="k")
    assert run(store, cfg, run_id) == "succeeded"
    assert len(gates) == 2 and len(acts) == 1
    assert "gate output unreadable" in acts[0]


def test_gate_same_rejection_hits_no_progress(monkeypatch, store, cfg, workdir):
    acts, gates = make_fake(monkeypatch, [{"pass": False, "reasons": ["same"]}] * 5)
    run_id = store.create_run("g", "exit 0", workdir, gate_prompt="k")
    assert run(store, cfg, run_id) == "failed"
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "no_progress"
    assert len(gates) == 3 and len(acts) == 2          # 3rd reject → stop before acting


def test_gate_cost_counts_into_budget(monkeypatch, store, cfg, workdir):
    make_fake(monkeypatch, [{"pass": False, "reasons": ["x"]}], gate_cost=3.0)
    run_id = store.create_run("g", "exit 0", workdir, gate_prompt="k",
                              max_cost_usd=2.5)
    assert run(store, cfg, run_id) == "failed"
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "budget_exceeded"
    assert store.get_run(run_id)["cost_total"] == pytest.approx(3.0)


def test_final_verify_also_gated(monkeypatch, store, cfg, workdir):
    """Fix on the last iteration → final verify passes → still has to clear the gate."""
    acts, gates = make_fake(
        monkeypatch, [{"pass": True}],
        fixer=lambda cwd: (Path(cwd) / "done.txt").write_text("ok"))
    run_id = store.create_run("g", "test -f done.txt", workdir,
                              gate_prompt="k", max_iterations=1)
    assert run(store, cfg, run_id) == "succeeded"
    assert len(acts) == 1 and len(gates) == 1
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "gate_passed"


def test_final_verify_gate_reject_fails_run(monkeypatch, store, cfg, workdir):
    acts, gates = make_fake(
        monkeypatch, [{"pass": False, "reasons": ["sloppy"]}],
        fixer=lambda cwd: (Path(cwd) / "done.txt").write_text("ok"))
    run_id = store.create_run("g", "test -f done.txt", workdir,
                              gate_prompt="k", max_iterations=1)
    assert run(store, cfg, run_id) == "failed"
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "gate_rejected"
