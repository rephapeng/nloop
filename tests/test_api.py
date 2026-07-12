"""Integration test API: TestClient jalanin lifespan (worker beneran, claude fake)."""
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
    cfg["paths"]["workspaces"] = str(tmp_path / "ws")
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
    raise AssertionError(f"run {run_id} nggak pernah {want} (terakhir: {status})")


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True, "app": "nloop"}


def test_dashboard_pages_served(client):
    r = client.get("/")
    assert r.status_code == 200 and 'data-page="index"' in r.text
    r = client.get("/run/apapun-id-nya")
    assert r.status_code == 200 and 'data-page="run"' in r.text
    r = client.get("/static/app.js")
    assert r.status_code == 200 and "EventSource" in r.text


def test_create_loop_runs_to_success(client):
    r = client.post("/api/loops", json={"goal": "bikin done.txt", "verify_cmd": VERIFY})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "queued"
    assert Path(body["workdir"]).is_dir()          # workdir auto-dibikinin

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
        "goal": "x", "verify_cmd": "exit 0", "workdir": "/path/ngawur/banget"})
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
    assert "verify" in events                      # replay kebaca
    assert "status" in events
    assert events[-1] == "done"                    # run final → stream ditutup


def test_sse_replay_with_after_cursor(client):
    r = client.post("/api/loops", json={"goal": "g", "verify_cmd": VERIFY}).json()
    wait_status(client, r["run_id"], "succeeded")

    # ambil id event terakhir lewat replay penuh dulu
    last_id = 0
    with client.stream("GET", f"/api/loops/{r['run_id']}/events") as resp:
        for line in resp.iter_lines():
            if line.startswith("id: "):
                last_id = int(line.removeprefix("id: "))
            if line == "event: done":
                break
    assert last_id > 0

    # reconnect pakai cursor → nggak ada event lama, langsung done
    with client.stream(
            "GET", f"/api/loops/{r['run_id']}/events?after={last_id}") as resp:
        lines = [l for l in resp.iter_lines() if l.startswith("event: ")]
    assert lines == ["event: done"]
