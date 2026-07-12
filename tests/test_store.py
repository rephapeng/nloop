from engine.store import Store


def make_store(tmp_path) -> Store:
    return Store(str(tmp_path / "test.db"))


def test_create_and_get_run(tmp_path):
    s = make_store(tmp_path)
    run_id = s.create_run("goal x", "exit 0", "/ws", max_iterations=3, max_cost_usd=1.5)
    run = s.get_run(run_id)
    assert run is not None
    assert run["goal"] == "goal x"
    assert run["verify_cmd"] == "exit 0"
    assert run["status"] == "queued"
    assert run["max_iterations"] == 3
    assert run["max_cost_usd"] == 1.5
    assert run["cost_total"] == 0
    assert s.get_run("nonexistent") is None


def test_status_transitions(tmp_path):
    s = make_store(tmp_path)
    run_id = s.create_run("g", "exit 0", "/ws")
    s.mark_started(run_id)
    assert s.get_run(run_id)["status"] == "running"
    assert s.get_run(run_id)["started_at"] is not None
    s.finish(run_id, "succeeded")
    run = s.get_run(run_id)
    assert run["status"] == "succeeded"
    assert run["ended_at"] is not None


def test_stop_flag(tmp_path):
    s = make_store(tmp_path)
    run_id = s.create_run("g", "exit 0", "/ws")
    assert not s.stop_requested(run_id)
    s.request_stop(run_id)
    assert s.stop_requested(run_id)


def test_bump_progress(tmp_path):
    s = make_store(tmp_path)
    run_id = s.create_run("g", "exit 0", "/ws")
    s.bump(run_id, cost_total=0.5, iterations_done=2, session_id="sess-a")
    run = s.get_run(run_id)
    assert run["cost_total"] == 0.5
    assert run["iterations_done"] == 2
    assert run["session_id"] == "sess-a"


def test_iterations_roundtrip(tmp_path):
    s = make_store(tmp_path)
    run_id = s.create_run("g", "exit 0", "/ws")
    s.add_iteration(
        run_id, idx=1, prompt="p1", result_text="r1", cost=0.1, turns=2,
        reason="success", verifier_passed=False, verifier_output="boom",
        started_at=1.0, ended_at=2.0,
    )
    s.add_iteration(
        run_id, idx=2, prompt="p2", result_text="r2", cost=0.2, turns=3,
        reason="success", verifier_passed=True, verifier_output="",
        started_at=3.0, ended_at=4.0,
    )
    its = s.iterations(run_id)
    assert [i["idx"] for i in its] == [1, 2]
    assert its[0]["verifier_output"] == "boom"
    assert its[1]["cost"] == 0.2


def test_events_since(tmp_path):
    s = make_store(tmp_path)
    run_id = s.create_run("g", "exit 0", "/ws")
    id1 = s.add_event(run_id, "verify", {"passed": False})
    s.add_event(run_id, "turn", {"text": "halo"})
    all_events = s.events_since(run_id)
    assert [e["type"] for e in all_events] == ["verify", "turn"]
    assert all_events[0]["payload"] == {"passed": False}
    # replay dari tengah (buat SSE reconnect)
    later = s.events_since(run_id, after_id=id1)
    assert [e["type"] for e in later] == ["turn"]


def test_events_isolated_per_run(tmp_path):
    s = make_store(tmp_path)
    a = s.create_run("g", "exit 0", "/ws")
    b = s.create_run("g", "exit 0", "/ws")
    s.add_event(a, "verify", {})
    assert s.events_since(b) == []


def test_list_runs_newest_first(tmp_path):
    s = make_store(tmp_path)
    a = s.create_run("older", "exit 0", "/ws")
    b = s.create_run("newer", "exit 0", "/ws")
    ids = [r["id"] for r in s.list_runs()]
    assert ids.index(b) < ids.index(a)
