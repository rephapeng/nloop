"""Fase 6: guardrails — loop rusak harus mati rapi, bukan muter/boros."""
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
    """Fake claude yang perilakunya di-script per attempt.

    behaviors: list of dict {subtype, ok?, cost?, fix?} — attempt ke-N pakai
    behaviors[N-1]; lewat itu pakai yang terakhir.
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


# ---- retry transient ----

def test_transient_error_retried_and_recovers(monkeypatch, store, cfg, workdir):
    calls = scripted_claude(monkeypatch, [
        {"subtype": "error_during_execution", "ok": False, "cost": 0.02},
        {"subtype": "success", "fix": True, "cost": 0.03},
    ])
    run_id = store.create_run("g", VERIFY, workdir)
    assert run(store, cfg, run_id) == "succeeded"
    assert len(calls) == 2                                     # attempt gagal + retry sukses
    r = store.get_run(run_id)
    assert r["iterations_done"] == 1                           # tetap 1 iterasi
    assert r["cost_total"] == pytest.approx(0.05)              # cost attempt gagal kehitung
    assert any("retry" in m for m in warn_logs(store, run_id))


def test_max_turns_not_treated_as_transient(monkeypatch, store, cfg, workdir):
    calls = scripted_claude(monkeypatch, [
        {"subtype": "error_max_turns", "ok": False},
        {"subtype": "success", "fix": True},
    ])
    run_id = store.create_run("g", VERIFY, workdir, max_iterations=3)
    assert run(store, cfg, run_id) == "succeeded"
    # error_max_turns BUKAN transient → nggak di-retry dalam iterasi yang sama;
    # iterasi berikutnya yang nyoba lagi
    assert not any("retry" in m for m in warn_logs(store, run_id))
    assert store.get_run(run_id)["iterations_done"] == 2
    assert len(calls) == 2


# ---- error beruntun & fatal ----

def test_consecutive_errors_fail_run(monkeypatch, store, cfg, workdir):
    calls = scripted_claude(monkeypatch, [
        {"subtype": "error_during_execution", "ok": False},
    ])
    # verifier variatif biar yang ketrigger guardrail error, bukan no_progress
    run_id = store.create_run("g", "date +%s%N; exit 1", workdir, max_iterations=10)
    assert run(store, cfg, run_id) == "failed"
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "claude_errors"
    # 2 iterasi (cap) x 2 attempt (retry transient) = 4 call claude
    assert len(calls) == 4


def test_claude_not_found_fails_fast(monkeypatch, store, cfg, workdir):
    calls = scripted_claude(monkeypatch, [{"subtype": "claude_not_found", "ok": False}])
    run_id = store.create_run("g", "exit 1", workdir, max_iterations=10)
    assert run(store, cfg, run_id) == "failed"
    assert len(calls) == 1                                     # fatal: no retry, no iterasi lanjut
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "claude_not_found"


# ---- no-progress ----

def test_progress_resets_no_progress_counter(monkeypatch, store, cfg, workdir):
    """Output verifier berubah tiap iterasi → nggak boleh kena stop no_progress."""
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
    assert last["reason"] == "max_iterations"                  # bukan no_progress
    assert len(calls) == 4                                     # semua iterasi kepake


# ---- budget alert ----

def test_budget_warning_emitted_once(monkeypatch, store, cfg, workdir):
    scripted_claude(monkeypatch, [{"subtype": "success", "cost": 0.9}])
    run_id = store.create_run("g", "date +%s%N; exit 1", workdir,
                              max_iterations=10, max_cost_usd=2.0)
    assert run(store, cfg, run_id) == "failed"
    budget_warns = [m for m in warn_logs(store, run_id) if "budget" in m]
    assert len(budget_warns) == 1                              # warning sekali, nggak spam
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "budget_exceeded"
