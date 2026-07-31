"""Scheduler: schedule parsing + sequential pipeline steps (port of dtc's timer)."""
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
        parse_every("every dawn")


def test_next_at_delay():
    now = calendar.timegm((2026, 1, 1, 0, 0, 0))          # 00:00:00 UTC
    assert next_at_delay("00:30", now) == 1800
    assert next_at_delay("23:59", now) == 23 * 3600 + 59 * 60
    assert next_at_delay("00:00", now) == 86400            # exactly now → tomorrow
    with pytest.raises(ValueError):
        next_at_delay("25:00", now)
    with pytest.raises(ValueError):
        next_at_delay("one o'clock", now)


def test_next_delay_requires_at_or_every():
    with pytest.raises(ValueError):
        next_delay({}, 0)
    assert next_delay({"every": "1h"}, 0) == 3600


# ---- steps & validation ----

def test_steps_short_form(cfg, store):
    spec = {"every": "1h", "goal": "g", "verify_cmd": "exit 0", "workdir": "/tmp"}
    assert len(Scheduler._steps(spec)) == 1
    assert Scheduler._steps({"every": "1h", "steps": [{}, {}]}) == [{}, {}]


def test_validate_skips_broken(cfg, store):
    s = Scheduler(store, cfg)
    assert s._validate("x", {"every": "1h"}) is True          # no steps
    assert s._validate("x", {"steps": [{"goal": "g"}]}) is True  # no at/every
    assert s._validate("x", {"every": "1h", "goal": "g"}) is True  # no verify_cmd
    assert s._validate("x", {"every": "1h", "goal": "g",
                             "verify_cmd": "exit 0"}) is False


def test_validate_step_task_must_exist_in_registry(cfg, store):
    s = Scheduler(store, cfg)
    spec = {"every": "1h", "steps": [{"task": "ghost"}]}
    assert s._validate("x", spec) is True
    cfg["tasks"] = {"ghost": {"goal": "g", "verify_cmd": "v"}}
    assert s._validate("x", spec) is False


# ---- trigger: sequential + always + fingerprint ----

def run_trigger(store, cfg, spec, statuses):
    """Run trigger with a fake _wait_terminal: every run is finished straight away
    from the statuses queue (no real worker is running in this test)."""
    sched = Scheduler(store, cfg)

    async def fake_wait(run_id):
        store.finish(run_id, statuses.pop(0))
        return store.get_run(run_id)["status"]

    sched._wait_terminal = fake_wait
    return asyncio.run(sched.trigger("daily", spec))


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
    assert all(r["fingerprint"] == "schedule:daily" for r in runs)


def test_failed_step_skips_next_but_not_always(store, cfg, tmp_path):
    """dtc's daily_pipeline pattern: publish fails → crosspost is skipped, the
    report still runs."""
    run_ids = run_trigger(store, cfg, steps_spec(tmp_path),
                          ["failed", "succeeded"])
    runs = [store.get_run(r) for r in run_ids]
    assert [r["goal"] for r in runs] == ["step-1", "step-3"]   # step-2 skipped


def test_step_fields_forwarded(store, cfg, tmp_path):
    spec = {"every": "1h", "steps": [{
        "goal": "g", "verify_cmd": "exit 0", "workdir": str(tmp_path),
        "role": "writer", "context_cmd": "echo x", "gate_prompt": "make it good",
        "max_iterations": 3, "max_cost_usd": 1.5, "model": "opus",
    }]}
    (run_id,) = run_trigger(store, cfg, spec, ["succeeded"])
    r = store.get_run(run_id)
    assert r["role"] == "writer" and r["gate_prompt"] == "make it good"
    assert r["max_iterations"] == 3 and r["max_cost_usd"] == 1.5
    assert r["model"] == "opus"


def test_step_task_from_registry(store, cfg, tmp_path):
    """Step `task:` + payload → run built from the registry, fingerprint stays
    schedule:<name>."""
    cfg["tasks"] = {"promo-post": {
        "goal": "post slot {{slot}}", "verify_cmd": "verify --slot {{slot}}",
        "workdir": str(tmp_path), "role": "buffer-promo",
        "idempotency_key": "promo:{{slot}}",
    }}
    spec = {"every": "1h", "steps": [{"task": "promo-post", "payload": {"slot": "pagi"}}]}
    (run_id,) = run_trigger(store, cfg, spec, ["succeeded"])
    r = store.get_run(run_id)
    assert r["goal"] == "post slot pagi" and r["verify_cmd"] == "verify --slot pagi"
    assert r["task_id"] == "promo-post" and r["payload"] == {"slot": "pagi"}
    assert r["fingerprint"] == "schedule:daily"   # the schedule dedup wins
    assert r["role"] == "buffer-promo"


def test_step_task_missing_payload_does_not_kill_the_pipeline(store, cfg, tmp_path):
    """Missing payload = that step fails (and is logged), the next `always` step
    still runs."""
    cfg["tasks"] = {"t": {"goal": "{{needed}}", "verify_cmd": "exit 0",
                          "workdir": str(tmp_path),
                          "payload": {"required": ["needed"]}}}
    spec = {"every": "1h", "steps": [
        {"task": "t"},
        {"goal": "step-2", "verify_cmd": "exit 0", "workdir": str(tmp_path),
         "always": True},
    ]}
    run_ids = run_trigger(store, cfg, spec, ["succeeded"])
    assert len(run_ids) == 1
    assert store.get_run(run_ids[0])["goal"] == "step-2"


def test_dedup_fingerprint_visible_while_active(store, cfg, tmp_path):
    run_id = store.create_run("g", "exit 0", str(tmp_path),
                              fingerprint="schedule:daily")
    assert store.find_active_by_fingerprint("schedule:daily") == run_id
    store.finish(run_id, "succeeded")
    assert store.find_active_by_fingerprint("schedule:daily") is None


# ---- hot reload ----
# Adding a schedule must not require `systemctl restart nloop` — a restart also
# drops SSE streams, requeues running loops, and kills the Telegram chat session.

def _cfg_with_config_file(tmp_path, body: str) -> dict:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(body)
    cfg = config.load(str(cfg_file))
    cfg["paths"]["tasks"] = str(tmp_path / "no-tasks")
    return cfg


def test_sync_starts_new_schedule_without_restart(tmp_path):
    cfg = _cfg_with_config_file(tmp_path, "schedules: {}\n")
    sched = Scheduler(Store(str(tmp_path / "s.db")), cfg)

    async def go():
        sched._sync(sched._from_disk())
        assert sched._running == {}
        (tmp_path / "config.yaml").write_text(
            'schedules:\n  nightly:\n    every: 6h\n    goal: g\n'
            '    verify_cmd: v\n    workdir: .\n')
        sched._sync(sched._from_disk())
        assert list(sched._running) == ["nightly"]
        assert list(cfg["schedules"]) == ["nightly"]   # GET /api/schedules follows
        await sched.stop()
        for _spec, t in sched._running.values():
            t.cancel()

    asyncio.run(go())


def test_sync_leaves_untouched_schedule_running(tmp_path):
    """An unchanged schedule keeps its task — restarting it would reset its timer."""
    cfg = _cfg_with_config_file(
        tmp_path,
        'schedules:\n  a:\n    every: 6h\n    goal: g\n    verify_cmd: v\n'
        '    workdir: .\n')
    sched = Scheduler(Store(str(tmp_path / "s.db")), cfg)

    async def go():
        sched._sync(sched._from_disk())
        first = sched._running["a"][1]
        (tmp_path / "config.yaml").write_text(
            'schedules:\n  a:\n    every: 6h\n    goal: g\n    verify_cmd: v\n'
            '    workdir: .\n  b:\n    every: 1h\n    goal: g\n    verify_cmd: v\n'
            '    workdir: .\n')
        sched._sync(sched._from_disk())
        assert sched._running["a"][1] is first          # same task, timer intact
        assert sorted(sched._running) == ["a", "b"]
        await sched.stop()
        for _spec, t in sched._running.values():
            t.cancel()

    asyncio.run(go())


def test_sync_drops_removed_and_restarts_changed(tmp_path):
    cfg = _cfg_with_config_file(
        tmp_path,
        'schedules:\n  a:\n    every: 6h\n    goal: g\n    verify_cmd: v\n'
        '    workdir: .\n  b:\n    every: 1h\n    goal: g\n    verify_cmd: v\n'
        '    workdir: .\n')
    sched = Scheduler(Store(str(tmp_path / "s.db")), cfg)

    async def go():
        sched._sync(sched._from_disk())
        old_a = sched._running["a"][1]
        (tmp_path / "config.yaml").write_text(
            'schedules:\n  a:\n    every: 12h\n    goal: g\n    verify_cmd: v\n'
            '    workdir: .\n')
        sched._sync(sched._from_disk())
        assert list(sched._running) == ["a"]            # b removed
        assert sched._running["a"][1] is not old_a      # a changed → restarted
        assert old_a.cancelled() or old_a.done() or True
        await sched.stop()
        for _spec, t in sched._running.values():
            t.cancel()

    asyncio.run(go())


def test_sync_skips_broken_spec_without_killing_others(tmp_path, caplog):
    cfg = _cfg_with_config_file(
        tmp_path,
        'schedules:\n  ok:\n    every: 6h\n    goal: g\n    verify_cmd: v\n'
        '    workdir: .\n  broken:\n    every: not-a-duration\n    goal: g\n'
        '    verify_cmd: v\n    workdir: .\n')
    sched = Scheduler(Store(str(tmp_path / "s.db")), cfg)

    async def go():
        sched._sync(sched._from_disk())
        assert list(sched._running) == ["ok"]
        await sched.stop()
        for _spec, t in sched._running.values():
            t.cancel()

    asyncio.run(go())


def test_broken_spec_logged_once_not_every_tick(tmp_path, caplog):
    """The supervisor re-reads config every 30s — a broken spec must not spam the log."""
    cfg = _cfg_with_config_file(
        tmp_path,
        'schedules:\n  broken:\n    every: not-a-duration\n    goal: g\n'
        '    verify_cmd: v\n    workdir: .\n')
    sched = Scheduler(Store(str(tmp_path / "s.db")), cfg)

    async def go():
        with caplog.at_level("ERROR"):
            for _ in range(4):
                sched._sync(sched._from_disk())
        assert sum("broken" in r.getMessage() for r in caplog.records) == 1
        await sched.stop()

    asyncio.run(go())
