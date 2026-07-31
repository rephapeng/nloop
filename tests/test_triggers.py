"""Fase 7: Sentry/PostHog webhook → reactive loop, dedup per fingerprint.
Fase 9b: repro-first (an issue run must write a repro script) + on_success_cmd."""
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine import config, loop
from engine.claude_cli import ClaudeResult
from engine.triggers import build_goal, compose_verify, extract_issue, repro_path
from server.app import create_app

SENTRY_PAYLOAD = {
    "action": "created",
    "data": {"issue": {
        "id": "sentry-123",
        "title": "TypeError: cannot read properties of undefined",
        "culprit": "app/checkout/page.tsx in handleSubmit",
        "web_url": "https://sentry.io/organizations/x/issues/123/",
    }},
}

POSTHOG_PAYLOAD = {
    "issue_id": "ph-42",
    "issue_name": "Uncaught ReferenceError: fetchCart is not defined",
    "issue_url": "https://us.posthog.com/project/1/error_tracking/ph-42",
}


# ---- extractor (unit) ----

def test_extract_sentry():
    i = extract_issue("sentry", SENTRY_PAYLOAD)
    assert i["fingerprint"] == "sentry:sentry-123"
    assert "TypeError" in i["title"]
    assert "checkout" in i["detail"]
    assert i["url"].startswith("https://sentry.io")


def test_extract_posthog():
    i = extract_issue("posthog", POSTHOG_PAYLOAD)
    assert i["fingerprint"] == "posthog:ph-42"
    assert "ReferenceError" in i["title"]


def test_extract_fallback_fingerprint_from_title():
    a = extract_issue("generic", {"message": "boom in module X"})
    b = extract_issue("generic", {"message": "boom in module X"})
    c = extract_issue("generic", {"message": "some other error"})
    assert a["fingerprint"] == b["fingerprint"]      # same title → same fp (dedup works)
    assert a["fingerprint"] != c["fingerprint"]
    assert a["title"] == "boom in module X"


def test_extract_garbage_payload_still_works():
    i = extract_issue("sentry", {})
    assert i["title"] == "(untitled issue)"
    assert i["fingerprint"].startswith("sentry:")


# ---- repro-first (unit) ----

def test_repro_path_sanitized():
    p = repro_path("sentry:abc/../123")
    assert p == ".nloop/repro/sentry-abc----123.sh"     # safe from path traversal


def test_compose_verify_forces_act_when_repro_missing():
    v = compose_verify("npm run build", ".nloop/repro/x.sh")
    assert v == "sh .nloop/repro/x.sh && (npm run build)"


def test_build_goal_with_repro_contract():
    issue = extract_issue("sentry", SENTRY_PAYLOAD)
    rpath = repro_path(issue["fingerprint"])
    g = build_goal("sentry", issue, repro_path=rpath,
                   verify_cmd=compose_verify("npm run build", rpath))
    assert "INVESTIGATE" in g and "REPRO" in g and "FIX" in g
    assert rpath in g
    assert "NOT a placeholder" in g                     # guards against a lying repro


def test_build_goal_without_repro_backward_compatible():
    issue = extract_issue("sentry", SENTRY_PAYLOAD)
    g = build_goal("sentry", issue)
    assert "reproduction test" in g and "REPRO:" not in g


# ---- endpoint (integration: real worker + fake claude) ----

@pytest.fixture
def project_dir(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    return d


@pytest.fixture
def client(monkeypatch, tmp_path, project_dir):
    async def fake_run(prompt, *, cwd, resume=None, **kwargs):
        await __import__("asyncio").sleep(0.15)      # leave room to dedup while active
        # The agent obeys the repro-first contract: write the repro script named in
        # the goal, then "fix" the bug.
        m = re.search(r"\.nloop/repro/\S+\.sh", prompt)
        if m:
            rp = Path(cwd) / m.group(0)
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text("test -f done.txt\n")     # repro: fails while the bug is there
        (Path(cwd) / "done.txt").write_text("ok")
        return ClaudeResult(ok=True, subtype="success", result_text="fixed",
                            session_id="s", cost_usd=0.01, num_turns=1)

    monkeypatch.setattr(loop.claude_cli, "run", fake_run)

    cfg = config.load("/nonexistent")
    cfg["paths"]["db"] = str(tmp_path / "trig.db")
    cfg["paths"]["scratch"] = str(tmp_path / "ws")
    cfg["loops"]["poll_interval_sec"] = 0.02
    cfg["triggers"] = {
        "token": "secret",
        "sentry": {"resolve": False, "url": "https://sentry.io"},
        "projects": {
            "demo": {"workdir": str(project_dir), "verify_cmd": "test -f done.txt",
                     "max_iterations": 3, "max_cost_usd": 1.0,
                     "on_success_cmd": "touch deployed.txt"},
        },
    }
    with TestClient(create_app(cfg)) as c:
        yield c


HOOK = "/api/hooks/sentry?project=demo&token=secret"


def wait_status(client, run_id, want, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get(f"/api/loops/{run_id}").json()["status"] == want:
            return
        time.sleep(0.03)
    raise AssertionError(f"run {run_id} never reached {want}")


def test_webhook_spawns_loop_that_fixes(client, project_dir):
    r = client.post(HOOK, json=SENTRY_PAYLOAD)
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False
    run = client.get(f"/api/loops/{body['run_id']}").json()
    assert "TypeError" in run["goal"]                # issue title lands in the goal
    assert "sentry.io" in run["goal"]                # link comes along
    assert "REPRO" in run["goal"]                    # repro-first contract lands in the goal
    assert run["verify_cmd"].startswith("sh .nloop/repro/")   # repro gate verifier
    wait_status(client, body["run_id"], "succeeded") # the loop really runs to the end

    # the repro script really got written & the release step (on_success_cmd) ran
    assert list(project_dir.glob(".nloop/repro/*.sh"))
    assert (project_dir / "deployed.txt").exists()
    detail = client.get(f"/api/loops/{body['run_id']}").json()
    assert detail["iterations_done"] >= 1            # forced to ACT (not a 0-iteration run)


def test_webhook_dedup_while_active_then_allows_after_done(client):
    first = client.post(HOOK, json=SENTRY_PAYLOAD).json()
    dup = client.post(HOOK, json=SENTRY_PAYLOAD)     # still queued/running
    assert dup.status_code == 200
    assert dup.json() == {"run_id": first["run_id"], "deduped": True,
                          "fingerprint": "sentry:sentry-123"}

    wait_status(client, first["run_id"], "succeeded")
    again = client.post(HOOK, json=SENTRY_PAYLOAD)   # issue shows up again once done
    assert again.status_code == 201                  # → allowed to spawn a new one
    assert again.json()["deduped"] is False


def test_webhook_auth_and_validation(client):
    assert client.post("/api/hooks/sentry?project=demo&token=wrong",
                       json=SENTRY_PAYLOAD).status_code == 401
    assert client.post("/api/hooks/sentry?project=ghost&token=secret",
                       json=SENTRY_PAYLOAD).status_code == 404
    r = client.post(HOOK, content=b"not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_webhook_posthog(client):
    r = client.post("/api/hooks/posthog?project=demo&token=secret",
                    json=POSTHOG_PAYLOAD)
    assert r.status_code == 201
    assert r.json()["fingerprint"] == "posthog:ph-42"


# ---- Fase 10: the issue run wires into the task model ----

def test_issue_run_recorded_as_task_issue_fix(client):
    r = client.post(HOOK, json=SENTRY_PAYLOAD).json()
    run = client.get(f"/api/loops/{r['run_id']}").json()
    assert run["task_id"] == "issue-fix"
    assert run["payload"]["source"] == "sentry"
    assert run["payload"]["fingerprint"] == "sentry:sentry-123"
    # the built-in task shows up in /api/tasks even though it isn't in the registry
    builtin = [t for t in client.get("/api/tasks").json() if t["id"] == "issue-fix"]
    assert builtin and builtin[0]["triggerable"] is False


def test_project_can_point_at_a_registry_task(monkeypatch, tmp_path, project_dir):
    """triggers.projects.<x>.task → the issue is handled by a custom task,
    payload = the issue."""
    async def fake_run(prompt, *, cwd, resume=None, **kwargs):
        (Path(cwd) / "done.txt").write_text("ok")
        return ClaudeResult(ok=True, subtype="success", result_text="ok",
                            session_id="s", cost_usd=0.01, num_turns=1)

    monkeypatch.setattr(loop.claude_cli, "run", fake_run)
    cfg = config.load("/nonexistent")
    cfg["paths"]["db"] = str(tmp_path / "trig2.db")
    cfg["paths"]["scratch"] = str(tmp_path / "ws2")
    cfg["paths"]["tasks"] = str(tmp_path / "tasks-empty")
    cfg["loops"]["poll_interval_sec"] = 0.02
    cfg["tasks"] = {"triage": {
        "goal": "Triage issue: {{title}} (from {{source}})",
        "verify_cmd": "test -f done.txt",
        "workdir": str(project_dir),
    }}
    cfg["triggers"] = {
        "token": None, "sentry": {"resolve": False, "url": "https://sentry.io"},
        "projects": {"demo": {"workdir": str(project_dir), "verify_cmd": "exit 0",
                              "task": "triage"}},
    }
    with TestClient(create_app(cfg)) as c:
        r = c.post("/api/hooks/sentry?project=demo", json=SENTRY_PAYLOAD).json()
        run = c.get(f"/api/loops/{r['run_id']}").json()
        assert run["task_id"] == "triage"
        assert run["goal"].startswith("Triage issue: TypeError")
        assert "from sentry" in run["goal"]
        assert run["fingerprint"] == "sentry:sentry-123"   # issue dedup still works
