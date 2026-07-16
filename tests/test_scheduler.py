"""Scheduler: parsing jadwal + pipeline steps sekuensial (port timer dtc)."""
import asyncio
import calendar

import pytest

from engine import config
from engine.scheduler import Scheduler, next_at_delay, next_delay, parse_every
from engine.store import Store


@pytest.fixture
def cfg():
    return config.load("/nonexistent")


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


# ---- parsing ----

def test_parse_every():
    assert parse_every("45s") == 45
    assert parse_every("30m") == 1800
    assert parse_every("6h") == 6 * 3600
    assert parse_every("1d") == 86400
    with pytest.raises(ValueError):
        parse_every("tiap subuh")


def test_next_at_delay():
    now = calendar.timegm((2026, 1, 1, 0, 0, 0))          # 00:00:00 UTC
    assert next_at_delay("00:30", now) == 1800
    assert next_at_delay("23:59", now) == 23 * 3600 + 59 * 60
    assert next_at_delay("00:00", now) == 86400            # persis sekarang → besok
    with pytest.raises(ValueError):
        next_at_delay("25:00", now)
    with pytest.raises(ValueError):
        next_at_delay("jam satu", now)


def test_next_delay_requires_at_or_every():
    with pytest.raises(ValueError):
        next_delay({}, 0)
    assert next_delay({"every": "1h"}, 0) == 3600


# ---- steps & validasi ----

def test_steps_short_form(cfg, store):
    spec = {"every": "1h", "goal": "g", "verify_cmd": "exit 0", "workdir": "/tmp"}
    assert len(Scheduler._steps(spec)) == 1
    assert Scheduler._steps({"every": "1h", "steps": [{}, {}]}) == [{}, {}]


def test_validate_skips_broken(cfg, store):
    s = Scheduler(store, cfg)
    assert s._validate("x", {"every": "1h"}) is True          # tanpa steps
    assert s._validate("x", {"steps": [{"goal": "g"}]}) is True  # tanpa at/every
    assert s._validate("x", {"every": "1h", "goal": "g"}) is False


# ---- trigger: sekuensial + always + fingerprint ----

def run_trigger(store, cfg, spec, statuses):
    """Jalankan trigger dengan _wait_terminal palsu: run langsung di-finish
    sesuai antrian statuses (worker beneran nggak jalan di test ini)."""
    sched = Scheduler(store, cfg)

    async def fake_wait(run_id):
        store.finish(run_id, statuses.pop(0))
        return store.get_run(run_id)["status"]

    sched._wait_terminal = fake_wait
    return asyncio.run(sched.trigger("harian", spec))


def steps_spec(tmp_path, n=3, always_last=True):
    steps = [{"goal": f"step-{i}", "verify_cmd": "exit 0", "workdir": str(tmp_path)}
             for i in range(1, n + 1)]
    if always_last:
        steps[-1]["always"] = True
    return {"every": "1h", "steps": steps}


def test_all_steps_run_when_all_succeed(store, cfg, tmp_path):
    run_ids = run_trigger(store, cfg, steps_spec(tmp_path),
                          ["succeeded", "succeeded", "succeeded"])
    assert len(run_ids) == 3
    runs = [store.get_run(r) for r in run_ids]
    assert [r["goal"] for r in runs] == ["step-1", "step-2", "step-3"]
    assert all(r["fingerprint"] == "schedule:harian" for r in runs)


def test_failed_step_skips_next_but_not_always(store, cfg, tmp_path):
    """Pola daily_pipeline dtc: publish gagal → crosspost skip, report tetap jalan."""
    run_ids = run_trigger(store, cfg, steps_spec(tmp_path),
                          ["failed", "succeeded"])
    runs = [store.get_run(r) for r in run_ids]
    assert [r["goal"] for r in runs] == ["step-1", "step-3"]   # step-2 di-skip


def test_step_fields_forwarded(store, cfg, tmp_path):
    spec = {"every": "1h", "steps": [{
        "goal": "g", "verify_cmd": "exit 0", "workdir": str(tmp_path),
        "role": "writer", "context_cmd": "echo x", "gate_prompt": "bagus",
        "max_iterations": 3, "max_cost_usd": 1.5, "model": "opus",
    }]}
    (run_id,) = run_trigger(store, cfg, spec, ["succeeded"])
    r = store.get_run(run_id)
    assert r["role"] == "writer" and r["gate_prompt"] == "bagus"
    assert r["max_iterations"] == 3 and r["max_cost_usd"] == 1.5
    assert r["model"] == "opus"


def test_dedup_fingerprint_visible_while_active(store, cfg, tmp_path):
    run_id = store.create_run("g", "exit 0", str(tmp_path),
                              fingerprint="schedule:harian")
    assert store.find_active_by_fingerprint("schedule:harian") == run_id
    store.finish(run_id, "succeeded")
    assert store.find_active_by_fingerprint("schedule:harian") is None
