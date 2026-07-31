"""API integration test: TestClient runs the lifespan (real worker, fake claude)."""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine import config, loop
from engine.claude_cli import ClaudeResult
from server.app import create_app

VERIFY = "test -f done.txt"


@pytest.fixture
def client(monkeypatch, tmp_path):
    async def fake_run(prompt, *, cwd, resume=None, **kwargs):
        (Path(cwd) / "done.txt").write_text("ok")
        return ClaudeResult(ok=True, subtype="success", result_text="fixed",
                            session_id="s", cost_usd=0.01, num_turns=1)

    monkeypatch.setattr(loop.claude_cli, "run", fake_run)

    cfg = config.load("/nonexistent")
    cfg["paths"]["db"] = str(tmp_path / "api.db")
    cfg["paths"]["workspaces"] = str(tmp_path / "wscfg")
    cfg["paths"]["scratch"] = str(tmp_path / "ws")
    cfg["loops"]["poll_interval_sec"] = 0.02
    with TestClient(create_app(cfg)) as c:
        yield c


def wait_status(client, run_id, want, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"/api/loops/{run_id}").json()["status"]
        if status == want:
            return status
        time.sleep(0.03)
    raise AssertionError(f"run {run_id} never became {want} (last: {status})")


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True, "app": "nloop"}


def test_dashboard_pages_served(client):
    for path, page in (("/", "index"), ("/run/whatever-the-id-is", "run"),
                       ("/tasks", "tasks"), ("/tasks/anything", "task"),
                       ("/schedules", "schedules")):
        r = client.get(path)
        assert r.status_code == 200, path
        assert f'data-page="{page}"' in r.text, path
    for asset in ("common.js", "runs.js", "run.js", "tasks.js", "schedules.js",
                  "style.css"):
        assert client.get(f"/static/{asset}").status_code == 200, asset
    assert "EventSource" in client.get("/static/run.js").text


def test_create_loop_runs_to_success(client):
    r = client.post("/api/loops", json={"goal": "create done.txt", "verify_cmd": VERIFY})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "queued"
    assert Path(body["workdir"]).is_dir()          # workdir created for us

    wait_status(client, body["run_id"], "succeeded")
    detail = client.get(f"/api/loops/{body['run_id']}").json()
    assert detail["iterations_done"] == 1
    assert len(detail["iterations"]) == 1
    assert detail["cost_total"] == pytest.approx(0.01)


def test_list_loops(client):
    a = client.post("/api/loops", json={"goal": "a", "verify_cmd": "exit 0"}).json()
    wait_status(client, a["run_id"], "succeeded")
    runs = client.get("/api/loops").json()
    assert any(r["id"] == a["run_id"] for r in runs)


def test_create_validates(client):
    assert client.post("/api/loops", json={"goal": "x"}).status_code == 422
    r = client.post("/api/loops", json={
        "goal": "x", "verify_cmd": "exit 0", "workdir": "/path/that/is/nonsense"})
    assert r.status_code == 400


def test_404s(client):
    assert client.get("/api/loops/ghost").status_code == 404
    assert client.post("/api/loops/ghost/stop").status_code == 404
    assert client.get("/api/loops/ghost/events").status_code == 404


def test_stop_endpoint_sets_flag(client):
    r = client.post("/api/loops", json={"goal": "g", "verify_cmd": VERIFY}).json()
    resp = client.post(f"/api/loops/{r['run_id']}/stop")
    assert resp.json()["stop_requested"] is True


def test_sse_replay_finished_run(client):
    r = client.post("/api/loops", json={"goal": "g", "verify_cmd": VERIFY}).json()
    wait_status(client, r["run_id"], "succeeded")

    events = []
    with client.stream("GET", f"/api/loops/{r['run_id']}/events") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("event: "):
                events.append(line.removeprefix("event: "))
            if line == "event: done":
                break
    assert "verify" in events                      # replay came through
    assert "status" in events
    assert events[-1] == "done"                    # run is final → stream closed


def test_create_with_unknown_role_400(client):
    r = client.post("/api/loops", json={
        "goal": "x", "verify_cmd": "exit 0", "role": "role-nonsense"})
    assert r.status_code == 400
    assert "role" in r.json()["detail"]


def test_schedules_empty_and_unknown_trigger(client):
    assert client.get("/api/schedules").json() == {}
    assert client.post("/api/schedules/ghost/trigger").status_code == 404


@pytest.fixture
def client_sched(monkeypatch, tmp_path):
    """Client with one schedule registered (manual trigger, no waiting for a clock)."""
    async def fake_run(prompt, *, cwd, resume=None, **kwargs):
        return ClaudeResult(ok=True, subtype="success", result_text="ok",
                            session_id="s", cost_usd=0.01, num_turns=1)

    monkeypatch.setattr(loop.claude_cli, "run", fake_run)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = config.load("/nonexistent")
    cfg["paths"]["db"] = str(tmp_path / "api.db")
    cfg["paths"]["workspaces"] = str(tmp_path / "wscfg")
    cfg["paths"]["scratch"] = str(ws)
    cfg["loops"]["poll_interval_sec"] = 0.02
    cfg["schedules"] = {"pipe": {"at": "23:59", "steps": [
        {"goal": "step-a", "verify_cmd": "exit 0", "workdir": str(ws)},
        {"goal": "step-b", "verify_cmd": "exit 0", "workdir": str(ws)},
    ]}}
    with TestClient(create_app(cfg)) as c:
        yield c


def test_schedule_listed_and_manual_trigger_runs_pipeline(client_sched):
    scheds = client_sched.get("/api/schedules").json()
    assert scheds["pipe"]["steps"] == 2 and scheds["pipe"]["at"] == "23:59"

    r = client_sched.post("/api/schedules/pipe/trigger")
    assert r.status_code == 202 and r.json()["triggered"] is True

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        runs = client_sched.get("/api/loops").json()
        done = [x for x in runs if x["status"] == "succeeded"]
        if len(done) == 2:
            break
        time.sleep(0.05)
    assert sorted(x["goal"] for x in done) == ["step-a", "step-b"]
    assert all(x["fingerprint"] == "schedule:pipe" for x in done)

    # last_tick: the step flow of the last tick, chronological, for the dashboard.
    tick = client_sched.get("/api/schedules").json()["pipe"]["last_tick"]
    assert [step["label"] for step in tick] == ["step-a", "step-b"]
    assert all(step["status"] == "succeeded" for step in tick)


@pytest.fixture
def client_tasks(monkeypatch, tmp_path):
    """Client with one task in the registry (Fase 10)."""
    async def fake_run(prompt, *, cwd, resume=None, **kwargs):
        (Path(cwd) / "done.txt").write_text("ok")
        return ClaudeResult(ok=True, subtype="success", result_text="ok",
                            session_id="s", cost_usd=0.01, num_turns=1)

    monkeypatch.setattr(loop.claude_cli, "run", fake_run)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = config.load("/nonexistent")
    cfg["paths"]["db"] = str(tmp_path / "api.db")
    cfg["paths"]["workspaces"] = str(tmp_path / "wscfg")
    cfg["paths"]["scratch"] = str(ws)
    cfg["paths"]["tasks"] = str(tmp_path / "tasks-empty")
    cfg["loops"]["poll_interval_sec"] = 0.02
    cfg["tasks"] = {"make-file": {
        "name": "Make a file",
        "goal": "create {{file}} in this workdir",
        "verify_cmd": "test -f {{file}}",
        "workdir": str(ws),
        "payload": {"required": ["file"]},
        "idempotency_key": "file:{{file}}",
    }}
    with TestClient(create_app(cfg)) as c:
        yield c


def test_list_tasks(client_tasks):
    items = client_tasks.get("/api/tasks").json()
    assert [t["id"] for t in items] == ["make-file"]
    assert items[0]["required"] == ["file"] and items[0]["triggerable"] is True
    assert items[0]["last_run"] is None


def test_new_task_file_picked_up_without_restart(client_tasks, tmp_path):
    """Write tasks/<id>.yaml while the server runs → it shows up in the registry."""
    assert [t["id"] for t in client_tasks.get("/api/tasks").json()] == ["make-file"]

    tasks_dir = tmp_path / "tasks-empty"
    tasks_dir.mkdir(exist_ok=True)
    (tasks_dir / "hello.yaml").write_text(
        f"name: Hello\ngoal: say hello\nverify_cmd: 'true'\nworkdir: {tmp_path}\n")

    items = client_tasks.get("/api/tasks").json()
    assert sorted(t["id"] for t in items) == ["hello", "make-file"]
    assert client_tasks.get("/api/tasks/hello").json()["name"] == "Hello"


def test_trigger_task_runs_to_success(client_tasks):
    r = client_tasks.post("/api/tasks/make-file/trigger",
                          json={"payload": {"file": "done.txt"}})
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False and body["task"] == "make-file"

    wait_status(client_tasks, body["run_id"], "succeeded")
    run = client_tasks.get(f"/api/loops/{body['run_id']}").json()
    assert run["task_id"] == "make-file"
    assert run["payload"] == {"file": "done.txt"}
    assert run["goal"] == "create done.txt in this workdir"
    assert run["fingerprint"] == "file:done.txt"


def test_trigger_task_missing_payload_400(client_tasks):
    r = client_tasks.post("/api/tasks/make-file/trigger", json={"payload": {}})
    assert r.status_code == 400 and "file" in r.json()["detail"]


def test_trigger_unknown_task_404(client_tasks):
    assert client_tasks.post("/api/tasks/ghost/trigger", json={}).status_code == 404
    assert client_tasks.get("/api/tasks/ghost").status_code == 404


def test_trigger_task_dedup_via_idempotency_key(client_tasks):
    body = {"payload": {"file": "never.txt"}}   # verify never passes → run stays stuck
    first = client_tasks.post("/api/tasks/make-file/trigger", json=body).json()
    again = client_tasks.post("/api/tasks/make-file/trigger", json=body)
    assert again.status_code == 200
    assert again.json() == {"run_id": first["run_id"], "task": "make-file",
                            "deduped": True, "idempotency_key": "file:never.txt"}


def test_create_loop_with_task(client_tasks):
    r = client_tasks.post("/api/loops", json={
        "task": "make-file", "payload": {"file": "done.txt"}, "max_iterations": 3})
    assert r.status_code == 201 and r.json()["task"] == "make-file"
    run = client_tasks.get(f"/api/loops/{r.json()['run_id']}").json()
    assert run["max_iterations"] == 3          # per-trigger override took effect


def test_create_loop_needs_task_or_goal(client_tasks):
    assert client_tasks.post("/api/loops", json={}).status_code == 422
    assert client_tasks.post("/api/loops", json={"goal": "g"}).status_code == 422


def test_task_detail_and_run_filter(client_tasks):
    r = client_tasks.post("/api/tasks/make-file/trigger",
                          json={"payload": {"file": "done.txt"}}).json()
    wait_status(client_tasks, r["run_id"], "succeeded")

    detail = client_tasks.get("/api/tasks/make-file").json()
    assert detail["goal"].startswith("create {{file}}")    # raw spec, not rendered
    assert [x["id"] for x in detail["runs"]] == [r["run_id"]]

    assert [x["id"] for x in client_tasks.get("/api/loops?task=make-file").json()] \
        == [r["run_id"]]
    assert client_tasks.get("/api/loops?task=other").json() == []
    assert client_tasks.get("/api/loops?status=succeeded").json()[0]["id"] == r["run_id"]


def test_sse_replay_with_after_cursor(client):
    r = client.post("/api/loops", json={"goal": "g", "verify_cmd": VERIFY}).json()
    wait_status(client, r["run_id"], "succeeded")

    # grab the id of the last event via a full replay first
    last_id = 0
    with client.stream("GET", f"/api/loops/{r['run_id']}/events") as resp:
        for line in resp.iter_lines():
            if line.startswith("id: "):
                last_id = int(line.removeprefix("id: "))
            if line == "event: done":
                break
    assert last_id > 0

    # reconnect with the cursor → no old events, straight to done
    with client.stream(
            "GET", f"/api/loops/{r['run_id']}/events?after={last_id}") as resp:
        lines = [l for l in resp.iter_lines() if l.startswith("event: ")]
    assert lines == ["event: done"]


# ---- Fase 12: trace endpoint for the waterfall ----

def test_trace_endpoint(client):
    r = client.post("/api/loops", json={"goal": "g", "verify_cmd": VERIFY}).json()
    wait_status(client, r["run_id"], "succeeded")
    t = client.get(f"/api/loops/{r['run_id']}/trace").json()

    kinds = [s["kind"] for s in t["spans"]]
    assert kinds[0] == "run" and "iteration" in kinds and "act" in kinds
    assert "verify" in kinds
    assert t["end"] >= t["start"]
    ids = {s["id"] for s in t["spans"]}
    assert all(s["parent_id"] in ids for s in t["spans"] if s["parent_id"])
    # verify duration is really measured (not estimated) since Fase 12
    verify = [s for s in t["spans"] if s["kind"] == "verify"]
    assert all(s["approx"] is False for s in verify)


def test_trace_404(client):
    assert client.get("/api/loops/ghost/trace").status_code == 404
