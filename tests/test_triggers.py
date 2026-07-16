"""Fase 7: webhook Sentry/PostHog → loop reaktif, dedup per fingerprint.
Fase 9b: repro-first (issue run wajib tulis script repro) + on_success_cmd."""
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
    a = extract_issue("generic", {"message": "boom di modul X"})
    b = extract_issue("generic", {"message": "boom di modul X"})
    c = extract_issue("generic", {"message": "error lain"})
    assert a["fingerprint"] == b["fingerprint"]      # judul sama → fp sama (dedup jalan)
    assert a["fingerprint"] != c["fingerprint"]
    assert a["title"] == "boom di modul X"


def test_extract_garbage_payload_still_works():
    i = extract_issue("sentry", {})
    assert i["title"] == "(untitled issue)"
    assert i["fingerprint"].startswith("sentry:")


# ---- repro-first (unit) ----

def test_repro_path_sanitized():
    p = repro_path("sentry:abc/../123")
    assert p == ".nloop/repro/sentry-abc----123.sh"     # aman dari path traversal


def test_compose_verify_forces_act_when_repro_missing():
    v = compose_verify("npm run build", ".nloop/repro/x.sh")
    assert v == "sh .nloop/repro/x.sh && (npm run build)"


def test_build_goal_with_repro_contract():
    issue = extract_issue("sentry", SENTRY_PAYLOAD)
    rpath = repro_path(issue["fingerprint"])
    g = build_goal("sentry", issue, repro_path=rpath,
                   verify_cmd=compose_verify("npm run build", rpath))
    assert "INVESTIGASI" in g and "REPRO" in g and "FIX" in g
    assert rpath in g
    assert "BUKAN placeholder" in g                     # anti repro bohongan


def test_build_goal_without_repro_backward_compatible():
    issue = extract_issue("sentry", SENTRY_PAYLOAD)
    g = build_goal("sentry", issue)
    assert "tulis test reproduksi" in g and "REPRO:" not in g


# ---- endpoint (integration, worker jalan + claude fake) ----

@pytest.fixture
def project_dir(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    return d


@pytest.fixture
def client(monkeypatch, tmp_path, project_dir):
    async def fake_run(prompt, *, cwd, resume=None, **kwargs):
        await __import__("asyncio").sleep(0.15)      # biar sempet ke-dedup saat aktif
        # Agent patuh kontrak repro-first: tulis script repro yang disebut di goal,
        # lalu "benerin" bug-nya.
        m = re.search(r"\.nloop/repro/\S+\.sh", prompt)
        if m:
            rp = Path(cwd) / m.group(0)
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text("test -f done.txt\n")     # repro: gagal selama bug ada
        (Path(cwd) / "done.txt").write_text("ok")
        return ClaudeResult(ok=True, subtype="success", result_text="fixed",
                            session_id="s", cost_usd=0.01, num_turns=1)

    monkeypatch.setattr(loop.claude_cli, "run", fake_run)

    cfg = config.load("/nonexistent")
    cfg["paths"]["db"] = str(tmp_path / "trig.db")
    cfg["paths"]["workspaces"] = str(tmp_path / "ws")
    cfg["loops"]["poll_interval_sec"] = 0.02
    cfg["triggers"] = {
        "token": "rahasia",
        "sentry": {"resolve": False, "url": "https://sentry.io"},
        "projects": {
            "demo": {"workdir": str(project_dir), "verify_cmd": "test -f done.txt",
                     "max_iterations": 3, "max_cost_usd": 1.0,
                     "on_success_cmd": "touch deployed.txt"},
        },
    }
    with TestClient(create_app(cfg)) as c:
        yield c


HOOK = "/api/hooks/sentry?project=demo&token=rahasia"


def wait_status(client, run_id, want, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get(f"/api/loops/{run_id}").json()["status"] == want:
            return
        time.sleep(0.03)
    raise AssertionError(f"run {run_id} nggak pernah {want}")


def test_webhook_spawns_loop_that_fixes(client, project_dir):
    r = client.post(HOOK, json=SENTRY_PAYLOAD)
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False
    run = client.get(f"/api/loops/{body['run_id']}").json()
    assert "TypeError" in run["goal"]                # judul issue masuk goal
    assert "sentry.io" in run["goal"]                # link ikut
    assert "REPRO" in run["goal"]                    # kontrak repro-first masuk goal
    assert run["verify_cmd"].startswith("sh .nloop/repro/")   # repro gate verifier
    wait_status(client, body["run_id"], "succeeded") # loop beneran jalan sampai beres

    # repro script beneran ketulis & langkah rilis (on_success_cmd) jalan
    assert list(project_dir.glob(".nloop/repro/*.sh"))
    assert (project_dir / "deployed.txt").exists()
    detail = client.get(f"/api/loops/{body['run_id']}").json()
    assert detail["iterations_done"] >= 1            # dipaksa ACT (nggak 0-iterasi)


def test_webhook_dedup_while_active_then_allows_after_done(client):
    first = client.post(HOOK, json=SENTRY_PAYLOAD).json()
    dup = client.post(HOOK, json=SENTRY_PAYLOAD)     # masih queued/running
    assert dup.status_code == 200
    assert dup.json() == {"run_id": first["run_id"], "deduped": True,
                          "fingerprint": "sentry:sentry-123"}

    wait_status(client, first["run_id"], "succeeded")
    again = client.post(HOOK, json=SENTRY_PAYLOAD)   # issue muncul lagi setelah kelar
    assert again.status_code == 201                  # → boleh spawn baru
    assert again.json()["deduped"] is False


def test_webhook_auth_and_validation(client):
    assert client.post("/api/hooks/sentry?project=demo&token=salah",
                       json=SENTRY_PAYLOAD).status_code == 401
    assert client.post("/api/hooks/sentry?project=ghost&token=rahasia",
                       json=SENTRY_PAYLOAD).status_code == 404
    r = client.post(HOOK, content=b"bukan json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_webhook_posthog(client):
    r = client.post("/api/hooks/posthog?project=demo&token=rahasia",
                    json=POSTHOG_PAYLOAD)
    assert r.status_code == 201
    assert r.json()["fingerprint"] == "posthog:ph-42"
