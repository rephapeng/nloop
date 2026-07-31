"""Fase 6: guardrails — a broken loop must die cleanly, not spin or burn money."""
import asyncio
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


def scripted_claude(monkeypatch, behaviors):
    """Fake claude whose behaviour is scripted per attempt.

    behaviors: list of dict {subtype, ok?, cost?, fix?} — attempt N uses
    behaviors[N-1]; past that it reuses the last one.
    """
    calls: list[str] = []

    async def fake_run(prompt, *, cwd, resume=None, **kwargs):
        b = behaviors[min(len(calls), len(behaviors) - 1)]
        calls.append(prompt)
        if b.get("fix"):
            (Path(cwd) / "done.txt").write_text("ok")
        return ClaudeResult(
            ok=b.get("ok", b.get("subtype") == "success"),
            subtype=b.get("subtype", "success"),
            result_text=b.get("subtype", "success"),
            session_id=f"sess-{len(calls)}",
            cost_usd=b.get("cost", 0.01),
            num_turns=1,
        )

    monkeypatch.setattr(loop.claude_cli, "run", fake_run)
    return calls


def run(store, cfg, run_id):
    return asyncio.run(loop.run_loop(run_id, store, cfg))


def warn_logs(store, run_id):
    return [e["payload"]["msg"] for e in store.events_since(run_id)
            if e["type"] == "log" and e["payload"].get("level") == "warn"]


VERIFY = "test -f done.txt"


# ---- transient retry ----

def test_transient_error_retried_and_recovers(monkeypatch, store, cfg, workdir):
    calls = scripted_claude(monkeypatch, [
        {"subtype": "error_during_execution", "ok": False, "cost": 0.02},
        {"subtype": "success", "fix": True, "cost": 0.03},
    ])
    run_id = store.create_run("g", VERIFY, workdir)
    assert run(store, cfg, run_id) == "succeeded"
    assert len(calls) == 2                                     # failed attempt + working retry
    r = store.get_run(run_id)
    assert r["iterations_done"] == 1                           # still just 1 iteration
    assert r["cost_total"] == pytest.approx(0.05)              # failed attempt's cost counts
    assert any("retry" in m for m in warn_logs(store, run_id))


def test_max_turns_not_treated_as_transient(monkeypatch, store, cfg, workdir):
    calls = scripted_claude(monkeypatch, [
        {"subtype": "error_max_turns", "ok": False},
        {"subtype": "success", "fix": True},
    ])
    run_id = store.create_run("g", VERIFY, workdir, max_iterations=3)
    assert run(store, cfg, run_id) == "succeeded"
    # error_max_turns is NOT transient → no retry inside the same iteration;
    # the next iteration is what tries again
    assert not any("retry" in m for m in warn_logs(store, run_id))
    assert store.get_run(run_id)["iterations_done"] == 2
    assert len(calls) == 2


# ---- consecutive errors & fatal ----

def test_consecutive_errors_fail_run(monkeypatch, store, cfg, workdir):
    calls = scripted_claude(monkeypatch, [
        {"subtype": "error_during_execution", "ok": False},
    ])
    # varying verifier output so the error guardrail fires, not no_progress
    run_id = store.create_run("g", "date +%s%N; exit 1", workdir, max_iterations=10)
    assert run(store, cfg, run_id) == "failed"
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "claude_errors"
    # 2 iterations (cap) x 2 attempts (transient retry) = 4 claude calls
    assert len(calls) == 4


def test_claude_not_found_fails_fast(monkeypatch, store, cfg, workdir):
    calls = scripted_claude(monkeypatch, [{"subtype": "claude_not_found", "ok": False}])
    run_id = store.create_run("g", "exit 1", workdir, max_iterations=10)
    assert run(store, cfg, run_id) == "failed"
    assert len(calls) == 1                                     # fatal: no retry, no next iter
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "claude_not_found"


# ---- no-progress ----

def test_progress_resets_no_progress_counter(monkeypatch, store, cfg, workdir):
    """Verifier output changes every iteration → must not trip the no_progress stop."""
    calls: list[str] = []

    async def fake_run(prompt, *, cwd, resume=None, **kwargs):
        calls.append(prompt)
        (Path(cwd) / "progress.txt").open("a").write(f"step{len(calls)}\n")
        return ClaudeResult(ok=True, subtype="success", result_text="step",
                            session_id="s", cost_usd=0.01, num_turns=1)

    monkeypatch.setattr(loop.claude_cli, "run", fake_run)
    run_id = store.create_run("g", "cat progress.txt 2>/dev/null; exit 1",
                              workdir, max_iterations=4)
    assert run(store, cfg, run_id) == "failed"
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "max_iterations"                  # not no_progress
    assert len(calls) == 4                                     # every iteration got used


# ---- budget alert ----

def test_budget_warning_emitted_once(monkeypatch, store, cfg, workdir):
    scripted_claude(monkeypatch, [{"subtype": "success", "cost": 0.9}])
    run_id = store.create_run("g", "date +%s%N; exit 1", workdir,
                              max_iterations=10, max_cost_usd=2.0)
    assert run(store, cfg, run_id) == "failed"
    budget_warns = [m for m in warn_logs(store, run_id) if "budget" in m]
    assert len(budget_warns) == 1                              # warned once, no spam
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "budget_exceeded"
