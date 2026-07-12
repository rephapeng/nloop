"""Unit test worker — claude di-fake; queue, semaphore, restart recovery beneran."""
import asyncio
from pathlib import Path

import pytest

from engine import config, loop
from engine.claude_cli import ClaudeResult
from engine.store import Store
from engine.worker import Worker


@pytest.fixture
def cfg():
    c = config.load("/nonexistent")
    c["loops"]["max_concurrent"] = 2
    c["loops"]["poll_interval_sec"] = 0.02
    return c


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


def make_ws(tmp_path, name: str) -> str:
    wd = tmp_path / name
    wd.mkdir()
    return str(wd)


async def wait_until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


def install_fake_claude(monkeypatch, *, delay=0.0, fail=False):
    """Fake: tulis done.txt (bikin verifier pass) + catat concurrency puncak."""
    state = {"active": 0, "max_active": 0, "calls": 0}

    async def fake_run(prompt, *, cwd, resume=None, **kwargs):
        state["calls"] += 1
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        try:
            if delay:
                await asyncio.sleep(delay)
            if fail:
                raise RuntimeError("claude meledak (disengaja)")
            (Path(cwd) / "done.txt").write_text("ok")
            return ClaudeResult(ok=True, subtype="success", result_text="fixed",
                                session_id="s", cost_usd=0.01, num_turns=1)
        finally:
            state["active"] -= 1

    monkeypatch.setattr(loop.claude_cli, "run", fake_run)
    return state


VERIFY = "test -f done.txt"


def test_semaphore_caps_concurrency(monkeypatch, store, cfg, tmp_path):
    state = install_fake_claude(monkeypatch, delay=0.2)

    async def main():
        ids = [
            store.create_run(f"goal {i}", VERIFY, make_ws(tmp_path, f"ws{i}"))
            for i in range(3)
        ]
        worker = Worker(store, cfg)
        task = asyncio.create_task(worker.run_forever())
        await wait_until(
            lambda: all(store.get_run(i)["status"] == "succeeded" for i in ids))
        await worker.stop()
        await task
        return ids

    ids = asyncio.run(main())
    assert state["calls"] == 3                 # semua run ke-proses
    assert state["max_active"] == 2            # ...tapi maksimal 2 barengan (cap)
    assert all(store.get_run(i)["status"] == "succeeded" for i in ids)


def test_restart_requeues_orphan_running(monkeypatch, store, cfg, tmp_path):
    """Simulasi crash: run ke-claim (status running) tapi prosesnya mati."""
    install_fake_claude(monkeypatch)
    run_id = store.create_run("g", VERIFY, make_ws(tmp_path, "ws"))
    claimed = store.claim_queued()
    assert claimed == run_id
    assert store.get_run(run_id)["status"] == "running"   # orphan

    async def main():  # "proses baru" boot
        worker = Worker(store, cfg)
        task = asyncio.create_task(worker.run_forever())
        await wait_until(lambda: store.get_run(run_id)["status"] == "succeeded")
        await worker.stop()
        await task

    asyncio.run(main())


def test_enqueue_while_worker_running(monkeypatch, store, cfg, tmp_path):
    install_fake_claude(monkeypatch)

    async def main():
        worker = Worker(store, cfg)
        task = asyncio.create_task(worker.run_forever())
        await asyncio.sleep(0.05)              # worker udah idle-polling
        run_id = store.create_run("g", VERIFY, make_ws(tmp_path, "ws"))
        await wait_until(lambda: store.get_run(run_id)["status"] == "succeeded")
        await worker.stop()
        await task

    asyncio.run(main())


def test_crashed_loop_marks_failed_worker_survives(monkeypatch, store, cfg, tmp_path):
    """Loop meledak → run failed, worker tetap hidup & proses run berikutnya."""
    state = install_fake_claude(monkeypatch, fail=True)
    bad = store.create_run("g", "exit 1", make_ws(tmp_path, "bad"))

    async def main():
        worker = Worker(store, cfg)
        task = asyncio.create_task(worker.run_forever())
        await wait_until(lambda: store.get_run(bad)["status"] == "failed")

        state_ok = install_fake_claude_ok()    # ganti fake jadi sehat
        good = store.create_run("g", VERIFY, make_ws(tmp_path, "good"))
        await wait_until(lambda: store.get_run(good)["status"] == "succeeded")
        await worker.stop()
        await task
        return good

    def install_fake_claude_ok():
        async def ok_run(prompt, *, cwd, resume=None, **kwargs):
            (Path(cwd) / "done.txt").write_text("ok")
            return ClaudeResult(ok=True, subtype="success", session_id="s",
                                cost_usd=0.01, num_turns=1)
        monkeypatch.setattr(loop.claude_cli, "run", ok_run)

    asyncio.run(main())
    events = store.events_since(bad)
    assert any("worker_error" in str(e["payload"].get("reason", ""))
               for e in events if e["type"] == "status")


def test_stop_leaves_queued_untouched(monkeypatch, store, cfg, tmp_path):
    """stop() nggak ngegantung & run yang belum ke-claim tetap 'queued' (tahan restart)."""
    install_fake_claude(monkeypatch)

    async def main():
        worker = Worker(store, cfg)
        task = asyncio.create_task(worker.run_forever())
        await worker.stop()
        await task
        # baru di-enqueue SETELAH worker mati → harus tetap antri
        run_id = store.create_run("g", VERIFY, make_ws(tmp_path, "ws"))
        return run_id

    run_id = asyncio.run(main())
    assert store.get_run(run_id)["status"] == "queued"


def test_claim_queued_oldest_first(store, tmp_path):
    a = store.create_run("older", "exit 0", "/ws")
    b = store.create_run("newer", "exit 0", "/ws")
    assert store.claim_queued() == a
    assert store.claim_queued() == b
    assert store.claim_queued() is None
