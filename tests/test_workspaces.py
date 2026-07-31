"""Workspaces: many tenants inside one nloop process.

What's guarded here: each workspace's config really is overlaid on top of the
global config, runs stay separated per tenant, and fingerprint dedup does NOT
leak across workspaces (two tenants may have a schedule/issue with the same name).
"""
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from engine import config, loop, telegram, workspaces
from engine.claude_cli import ClaudeResult
from engine.store import Store
from server.app import create_app


def write_ws(root: Path, name: str, spec: dict) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(yaml.safe_dump(spec))


# ---- loader ----

def test_without_workspaces_dir_falls_back_to_old_mode(tmp_path):
    cfg = config.load("/nonexistent")
    cfg["paths"]["workspaces"] = str(tmp_path / "does-not-exist")
    out = workspaces.load_all(cfg)
    assert list(out) == [workspaces.DEFAULT]
    assert out[workspaces.DEFAULT]["workspace"] == workspaces.DEFAULT
    assert workspaces.primary(out) == workspaces.DEFAULT


def test_workspace_config_overlaid_on_global(tmp_path):
    root = tmp_path / "ws"
    write_ws(root, "onecookie", {
        "label": "OneCookie", "primary": True,
        "loops": {"max_cost_usd": 1.5},          # overrides part, rest follows global
        "schedules": {"promo": {"at": "01:00", "goal": "x", "verify_cmd": "exit 0"}},
    })
    write_ws(root, "jetorbit", {"label": "Jetorbit"})

    cfg = config.load("/nonexistent")
    cfg["paths"]["workspaces"] = str(root)
    out = workspaces.load_all(cfg)

    assert sorted(out) == ["jetorbit", "onecookie"]
    one = out["onecookie"]
    assert one["loops"]["max_cost_usd"] == 1.5                      # overridden
    assert one["loops"]["max_iterations"] == cfg["loops"]["max_iterations"]  # from global
    assert list(one["schedules"]) == ["promo"]
    assert out["jetorbit"]["schedules"] == {}      # other tenants don't leak in
    assert workspaces.primary(out) == "onecookie"  # `primary: true`


def test_server_section_is_locked(tmp_path):
    root = tmp_path / "ws"
    write_ws(root, "a", {"server": {"port": 9999}})
    cfg = config.load("/nonexistent")
    cfg["paths"]["workspaces"] = str(root)
    out = workspaces.load_all(cfg)
    assert out["a"]["server"]["port"] == cfg["server"]["port"]  # not 9999


def test_tasks_dir_inside_workspace_wins(tmp_path):
    root = tmp_path / "ws"
    write_ws(root, "a", {})
    tasks_dir = root / "a" / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "local.yaml").write_text(
        yaml.safe_dump({"goal": "g", "verify_cmd": "exit 0"}))
    cfg = config.load("/nonexistent")
    cfg["paths"]["workspaces"] = str(root)
    out = workspaces.load_all(cfg)
    assert "local" in out["a"]["tasks"]
    assert out["a"]["paths"]["tasks"] == str(tasks_dir)


def test_broken_workspace_is_skipped_not_fatal(tmp_path):
    root = tmp_path / "ws"
    write_ws(root, "healthy", {"label": "ok"})
    (root / "broken").mkdir()
    (root / "broken" / "config.yaml").write_text("[this: is not a mapping")
    cfg = config.load("/nonexistent")
    cfg["paths"]["workspaces"] = str(root)
    assert list(workspaces.load_all(cfg)) == ["healthy"]


def test_primary_fallback_when_nobody_marks_one(tmp_path):
    root = tmp_path / "ws"
    write_ws(root, "b", {})
    write_ws(root, "a", {})
    cfg = config.load("/nonexistent")
    cfg["paths"]["workspaces"] = str(root)
    out = workspaces.load_all(cfg)
    assert workspaces.primary(out) == "a"   # no marker → stable ordering


# ---- store ----

def test_fingerprint_dedup_doesnt_leak_across_workspaces(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    fp = "schedule:promo"
    a = s.create_run("a", "exit 0", "/ws", fingerprint=fp, workspace="onecookie")
    b = s.create_run("b", "exit 0", "/ws", fingerprint=fp, workspace="jetorbit")
    assert s.find_active_by_fingerprint(fp, workspace="onecookie") == a
    assert s.find_active_by_fingerprint(fp, workspace="jetorbit") == b
    # without a scope = across workspaces (the old path)
    assert s.find_active_by_fingerprint(fp) in (a, b)
    assert [r["id"] for r in s.list_runs(workspace="jetorbit")] == [b]


def test_old_runs_adopted_by_primary_workspace(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    old = s.create_run("pre-workspace run", "exit 0", "/ws")   # workspace NULL
    assert s.get_run(old)["workspace"] is None
    assert s.adopt_orphan_runs("onecookie") == 1
    assert s.get_run(old)["workspace"] == "onecookie"
    assert [r["id"] for r in s.list_runs(workspace="onecookie")] == [old]
    assert s.adopt_orphan_runs("onecookie") == 0               # idempotent


# ---- telegram: one bot per workspace ----

def test_env_token_separate_per_workspace():
    # primary keeps the old names — a setup that already works needs no change
    assert telegram.env_names({}, "onecookie", primary=True) == (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS")
    # other tenants MUST have their own token (same token = 409 Conflict on getUpdates)
    assert telegram.env_names({}, "jetorbit") == (
        "TELEGRAM_BOT_TOKEN_JETORBIT", "TELEGRAM_ALLOWED_CHAT_IDS_JETORBIT")
    assert telegram.env_names({"token_env": "MY_TOKEN"}, "jetorbit")[0] == "MY_TOKEN"


def test_chat_sessions_separate_per_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Store(str(tmp_path / "t.db"))
    cfg = config.load("/nonexistent")
    a = telegram.TelegramBot({**cfg, "workspace": "onecookie"}, store)
    b = telegram.TelegramBot({**cfg, "workspace": "jetorbit"}, store)
    assert a._sid_path(42) != b._sid_path(42)   # same chat id, different context


# ---- API ----

@pytest.fixture
def client_ws(monkeypatch, tmp_path):
    """Two workspaces: 'onecookie' (primary) and 'jetorbit'."""
    async def fake_run(prompt, *, cwd, resume=None, **kwargs):
        (Path(cwd) / "done.txt").write_text("ok")
        return ClaudeResult(ok=True, subtype="success", result_text="ok",
                            session_id="s", cost_usd=0.01, num_turns=1)

    monkeypatch.setattr(loop.claude_cli, "run", fake_run)
    root = tmp_path / "wscfg"
    proj = tmp_path / "proj"
    proj.mkdir()
    write_ws(root, "onecookie", {
        "label": "OneCookie", "primary": True,
        "tasks": {"make-file": {"goal": "create done.txt", "verify_cmd": "exit 0",
                                "workdir": str(proj)}},
        "triggers": {"token": "secret-onecookie",
                     "projects": {"demo": {"workdir": str(proj),
                                           "verify_cmd": "exit 0", "repro": False}}},
    })
    write_ws(root, "jetorbit", {"label": "Jetorbit"})

    cfg = config.load("/nonexistent")
    cfg["paths"]["db"] = str(tmp_path / "api.db")
    cfg["paths"]["workspaces"] = str(root)
    cfg["paths"]["scratch"] = str(tmp_path / "scratch")
    cfg["loops"]["poll_interval_sec"] = 0.02
    with TestClient(create_app(cfg)) as c:
        yield c


def test_list_workspaces(client_ws):
    body = client_ws.get("/api/workspaces").json()
    assert body["primary"] == "onecookie"
    names = [w["name"] for w in body["workspaces"]]
    assert names == ["jetorbit", "onecookie"]
    one = next(w for w in body["workspaces"] if w["name"] == "onecookie")
    assert one["primary"] is True and one["label"] == "OneCookie"
    assert one["projects"] == ["demo"] and one["tasks"] == 1


def test_runs_separated_per_workspace(client_ws):
    r = client_ws.post("/api/loops?workspace=jetorbit",
                       json={"goal": "g", "verify_cmd": "exit 0"})
    assert r.status_code == 201 and r.json()["workspace"] == "jetorbit"
    run_id = r.json()["run_id"]

    assert [x["id"] for x in client_ws.get("/api/loops?workspace=jetorbit").json()] == [run_id]
    assert client_ws.get("/api/loops?workspace=onecookie").json() == []
    # no param = the primary workspace (old clients keep working)
    assert client_ws.get("/api/loops").json() == []


def test_unknown_workspace_404(client_ws):
    assert client_ws.get("/api/loops?workspace=ghost").status_code == 404


def test_task_registry_per_workspace(client_ws):
    assert [t["id"] for t in client_ws.get("/api/tasks?workspace=onecookie").json()] \
        == ["make-file"]
    assert client_ws.get("/api/tasks?workspace=jetorbit").json() == []


def test_webhook_token_checked_per_workspace(client_ws):
    payload = {"data": {"issue": {"id": "77", "title": "boom"}}}
    # onecookie's project & token don't apply in another workspace
    assert client_ws.post(
        "/api/hooks/sentry?workspace=jetorbit&project=demo&token=secret-onecookie",
        json=payload).status_code == 404
    r = client_ws.post(
        "/api/hooks/sentry?workspace=onecookie&project=demo&token=secret-onecookie",
        json=payload)
    assert r.status_code == 201 and r.json()["workspace"] == "onecookie"

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = client_ws.get(f"/api/loops/{r.json()['run_id']}").json()
        if run["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.03)
    assert run["workspace"] == "onecookie"
