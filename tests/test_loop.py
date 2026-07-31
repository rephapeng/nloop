"""Unit tests for the loop core — claude is faked (free & deterministic).

The fake claude is injected via monkeypatch into engine.loop.claude_cli.run;
the verifier & store stay REAL (temp files + SQLite).
"""
import asyncio
from pathlib import Path

import pytest

from engine import config, loop
from engine.claude_cli import ClaudeResult
from engine.store import Store


@pytest.fixture
def cfg():
    c = config.load("/nonexistent")          # pure defaults
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


def make_fake_claude(monkeypatch, fixer=None, cost=0.01):
    """Fake claude_cli.run: record the prompt, optionally 'fix' the workdir."""
    calls: list[str] = []

    async def fake_run(prompt, *, cwd, resume=None, **kwargs):
        calls.append(prompt)
        if fixer:
            fixer(cwd)
        return ClaudeResult(
            ok=True, subtype="success", result_text=f"action-{len(calls)}",
            session_id=f"sess-{len(calls)}", cost_usd=cost, num_turns=2,
        )

    monkeypatch.setattr(loop.claude_cli, "run", fake_run)
    return calls


def run(store, cfg, run_id):
    return asyncio.run(loop.run_loop(run_id, store, cfg))


# ---- happy path ----

def test_succeeds_after_fix(monkeypatch, store, cfg, workdir):
    calls = make_fake_claude(
        monkeypatch, fixer=lambda cwd: (Path(cwd) / "fixed.txt").write_text("ok"))
    run_id = store.create_run("create fixed.txt", "test -f fixed.txt", workdir)

    assert run(store, cfg, run_id) == "succeeded"
    assert len(calls) == 1                        # one act is enough

    r = store.get_run(run_id)
    assert r["status"] == "succeeded"
    assert r["iterations_done"] == 1
    assert r["cost_total"] == pytest.approx(0.01)
    assert r["session_id"] == "sess-1"

    its = store.iterations(run_id)
    assert len(its) == 1 and its[0]["reason"] == "success"

    types = [e["type"] for e in store.events_since(run_id)]
    assert types.count("verify") == 2             # fail, then pass
    assert types[-1] == "status"


def test_already_passing_skips_claude(monkeypatch, store, cfg, workdir):
    calls = make_fake_claude(monkeypatch)
    run_id = store.create_run("noop", "exit 0", workdir)
    assert run(store, cfg, run_id) == "succeeded"
    assert calls == []                            # observe first, don't burn money


def test_fix_on_last_iteration_still_succeeds(monkeypatch, store, cfg, workdir):
    """The last iteration's action still gets verified (final verify)."""
    state = {"n": 0}

    def slow_fixer(cwd):
        state["n"] += 1
        if state["n"] >= 2:
            (Path(cwd) / "fixed.txt").write_text("ok")

    make_fake_claude(monkeypatch, fixer=slow_fixer)
    run_id = store.create_run("g", "test -f fixed.txt", workdir, max_iterations=2)
    assert run(store, cfg, run_id) == "succeeded"


# ---- guardrails ----

def test_budget_exceeded(monkeypatch, store, cfg, workdir):
    calls = make_fake_claude(monkeypatch, cost=1.0)   # never fixes anything
    # varying verifier output so the no-progress guardrail doesn't fire first
    run_id = store.create_run("g", "date +%s%N; exit 1", workdir,
                              max_iterations=10, max_cost_usd=2.5)
    assert run(store, cfg, run_id) == "failed"
    assert len(calls) == 3                            # 1.0+1.0+1.0 > 2.5 → stop
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "budget_exceeded"


def test_max_iterations(monkeypatch, store, cfg, workdir):
    calls = make_fake_claude(monkeypatch)
    run_id = store.create_run("g", "exit 1", workdir, max_iterations=2)
    assert run(store, cfg, run_id) == "failed"
    assert len(calls) == 2
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "max_iterations"


def test_stop_requested(monkeypatch, store, cfg, workdir):
    calls = make_fake_claude(monkeypatch)
    run_id = store.create_run("g", "exit 1", workdir)
    store.request_stop(run_id)
    assert run(store, cfg, run_id) == "stopped"
    assert calls == []


# ---- recover / anti-repeat ----

def test_no_progress_hint_then_auto_stop(monkeypatch, store, cfg, workdir):
    calls = make_fake_claude(monkeypatch)
    run_id = store.create_run("g", "echo always-the-same; exit 1", workdir,
                              max_iterations=10)
    assert run(store, cfg, run_id) == "failed"
    assert loop.NO_PROGRESS_HINT not in calls[0]      # iter 1: nothing to compare yet
    assert loop.NO_PROGRESS_HINT in calls[1]          # iter 2: identical output → hint
    assert len(calls) == 2                            # iter 3: 2x in a row → STOP, no act
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "no_progress"


def test_journal_injected_into_prompt(monkeypatch, store, cfg, workdir):
    calls = make_fake_claude(monkeypatch)
    run_id = store.create_run("g", "exit 1", workdir, max_iterations=3)
    run(store, cfg, run_id)
    assert "WHAT HAS ALREADY BEEN TRIED" not in calls[0]
    assert "WHAT HAS ALREADY BEEN TRIED" in calls[1]         # iter-1 journal carried in
    assert "action-1" in calls[1]


# ---- memory Tier 1 ----

def test_claudemd_seeded_with_goal(monkeypatch, store, cfg, workdir):
    make_fake_claude(monkeypatch)
    run_id = store.create_run("this specific goal", "exit 0", workdir)
    run(store, cfg, run_id)
    text = (Path(workdir) / "CLAUDE.md").read_text()
    assert text.splitlines()[0] == "# GOAL: this specific goal"


def test_existing_claudemd_not_overwritten(monkeypatch, store, cfg, workdir):
    (Path(workdir) / "CLAUDE.md").write_text("# user's own, don't touch\n")
    make_fake_claude(monkeypatch)
    run_id = store.create_run("g", "exit 0", workdir)
    run(store, cfg, run_id)
    assert (Path(workdir) / "CLAUDE.md").read_text() == "# user's own, don't touch\n"


def test_run_not_found(store, cfg):
    with pytest.raises(ValueError):
        asyncio.run(loop.run_loop("ghost", store, cfg))
