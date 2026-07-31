"""Workspace: banyak tenant dalam satu proses nloop.

Yang dijaga di sini: config tiap workspace beneran ke-overlay di atas config
global, run kepisah per tenant, dan dedup fingerprint NGGAK bocor antar
workspace (dua tenant boleh punya schedule/issue dengan nama sama).
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

def test_tanpa_direktori_workspaces_balik_ke_mode_lama(tmp_path):
    cfg = config.load("/nonexistent")
    cfg["paths"]["workspaces"] = str(tmp_path / "nggak-ada")
    out = workspaces.load_all(cfg)
    assert list(out) == [workspaces.DEFAULT]
    assert out[workspaces.DEFAULT]["workspace"] == workspaces.DEFAULT
    assert workspaces.primary(out) == workspaces.DEFAULT


def test_config_workspace_dioverlay_di_atas_global(tmp_path):
    root = tmp_path / "ws"
    write_ws(root, "onecookie", {
        "label": "OneCookie", "primary": True,
        "loops": {"max_cost_usd": 1.5},          # nimpa sebagian, sisanya ikut global
        "schedules": {"promo": {"at": "01:00", "goal": "x", "verify_cmd": "exit 0"}},
    })
    write_ws(root, "jetorbit", {"label": "Jetorbit"})

    cfg = config.load("/nonexistent")
    cfg["paths"]["workspaces"] = str(root)
    out = workspaces.load_all(cfg)

    assert sorted(out) == ["jetorbit", "onecookie"]
    one = out["onecookie"]
    assert one["loops"]["max_cost_usd"] == 1.5                      # di-override
    assert one["loops"]["max_iterations"] == cfg["loops"]["max_iterations"]  # ikut global
    assert list(one["schedules"]) == ["promo"]
    assert out["jetorbit"]["schedules"] == {}      # tenant lain nggak kebawa
    assert workspaces.primary(out) == "onecookie"  # `primary: true`


def test_section_server_dikunci(tmp_path):
    root = tmp_path / "ws"
    write_ws(root, "a", {"server": {"port": 9999}})
    cfg = config.load("/nonexistent")
    cfg["paths"]["workspaces"] = str(root)
    out = workspaces.load_all(cfg)
    assert out["a"]["server"]["port"] == cfg["server"]["port"]  # bukan 9999


def test_tasks_dir_dalam_workspace_menang(tmp_path):
    root = tmp_path / "ws"
    write_ws(root, "a", {})
    tasks_dir = root / "a" / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "lokal.yaml").write_text(
        yaml.safe_dump({"goal": "g", "verify_cmd": "exit 0"}))
    cfg = config.load("/nonexistent")
    cfg["paths"]["workspaces"] = str(root)
    out = workspaces.load_all(cfg)
    assert "lokal" in out["a"]["tasks"]
    assert out["a"]["paths"]["tasks"] == str(tasks_dir)


def test_workspace_rusak_di_skip_bukan_matiin_server(tmp_path):
    root = tmp_path / "ws"
    write_ws(root, "sehat", {"label": "ok"})
    (root / "rusak").mkdir()
    (root / "rusak" / "config.yaml").write_text("[ini: bukan mapping")
    cfg = config.load("/nonexistent")
    cfg["paths"]["workspaces"] = str(root)
    assert list(workspaces.load_all(cfg)) == ["sehat"]


def test_primary_fallback_kalau_nggak_ada_yang_nandain(tmp_path):
    root = tmp_path / "ws"
    write_ws(root, "b", {})
    write_ws(root, "a", {})
    cfg = config.load("/nonexistent")
    cfg["paths"]["workspaces"] = str(root)
    out = workspaces.load_all(cfg)
    assert workspaces.primary(out) == "a"   # nggak ada penanda → urutan stabil


# ---- store ----

def test_dedup_fingerprint_nggak_bocor_antar_workspace(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    fp = "schedule:promo"
    a = s.create_run("a", "exit 0", "/ws", fingerprint=fp, workspace="onecookie")
    b = s.create_run("b", "exit 0", "/ws", fingerprint=fp, workspace="jetorbit")
    assert s.find_active_by_fingerprint(fp, workspace="onecookie") == a
    assert s.find_active_by_fingerprint(fp, workspace="jetorbit") == b
    # tanpa scope = lintas workspace (jalur lama)
    assert s.find_active_by_fingerprint(fp) in (a, b)
    assert [r["id"] for r in s.list_runs(workspace="jetorbit")] == [b]


def test_run_lama_diadopsi_workspace_primary(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    old = s.create_run("run pra-workspace", "exit 0", "/ws")   # workspace NULL
    assert s.get_run(old)["workspace"] is None
    assert s.adopt_orphan_runs("onecookie") == 1
    assert s.get_run(old)["workspace"] == "onecookie"
    assert [r["id"] for r in s.list_runs(workspace="onecookie")] == [old]
    assert s.adopt_orphan_runs("onecookie") == 0               # idempoten


# ---- telegram: satu bot per workspace ----

def test_env_token_terpisah_per_workspace():
    # primary tetap pakai nama lama — setup yang udah jalan nggak perlu diubah
    assert telegram.env_names({}, "onecookie", primary=True) == (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS")
    # tenant lain WAJIB token sendiri (token sama = 409 Conflict di getUpdates)
    assert telegram.env_names({}, "jetorbit") == (
        "TELEGRAM_BOT_TOKEN_JETORBIT", "TELEGRAM_ALLOWED_CHAT_IDS_JETORBIT")
    assert telegram.env_names({"token_env": "MY_TOKEN"}, "jetorbit")[0] == "MY_TOKEN"


def test_sesi_chat_kepisah_per_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Store(str(tmp_path / "t.db"))
    cfg = config.load("/nonexistent")
    a = telegram.TelegramBot({**cfg, "workspace": "onecookie"}, store)
    b = telegram.TelegramBot({**cfg, "workspace": "jetorbit"}, store)
    assert a._sid_path(42) != b._sid_path(42)   # chat id sama, konteks beda


# ---- API ----

@pytest.fixture
def client_ws(monkeypatch, tmp_path):
    """Dua workspace: 'onecookie' (primary) dan 'jetorbit'."""
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
        "tasks": {"buat-file": {"goal": "bikin done.txt", "verify_cmd": "exit 0",
                                "workdir": str(proj)}},
        "triggers": {"token": "rahasia-onecookie",
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


def test_run_kepisah_per_workspace(client_ws):
    r = client_ws.post("/api/loops?workspace=jetorbit",
                       json={"goal": "g", "verify_cmd": "exit 0"})
    assert r.status_code == 201 and r.json()["workspace"] == "jetorbit"
    run_id = r.json()["run_id"]

    assert [x["id"] for x in client_ws.get("/api/loops?workspace=jetorbit").json()] == [run_id]
    assert client_ws.get("/api/loops?workspace=onecookie").json() == []
    # tanpa param = workspace primary (client lama tetap jalan)
    assert client_ws.get("/api/loops").json() == []


def test_workspace_nggak_dikenal_404(client_ws):
    assert client_ws.get("/api/loops?workspace=hantu").status_code == 404


def test_task_registry_per_workspace(client_ws):
    assert [t["id"] for t in client_ws.get("/api/tasks?workspace=onecookie").json()] \
        == ["buat-file"]
    assert client_ws.get("/api/tasks?workspace=jetorbit").json() == []


def test_webhook_token_dicek_per_workspace(client_ws):
    payload = {"data": {"issue": {"id": "77", "title": "boom"}}}
    # project & token milik onecookie nggak berlaku di workspace lain
    assert client_ws.post(
        "/api/hooks/sentry?workspace=jetorbit&project=demo&token=rahasia-onecookie",
        json=payload).status_code == 404
    r = client_ws.post(
        "/api/hooks/sentry?workspace=onecookie&project=demo&token=rahasia-onecookie",
        json=payload)
    assert r.status_code == 201 and r.json()["workspace"] == "onecookie"

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = client_ws.get(f"/api/loops/{r.json()['run_id']}").json()
        if run["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.03)
    assert run["workspace"] == "onecookie"
