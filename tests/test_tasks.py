"""Task registry: load, render template, validasi payload, trigger + dedup."""
import pytest

from engine import config, tasks
from engine.store import Store

TASK = {
    "name": "Promo post",
    "payload": {"required": ["slot"], "defaults": {"n": 10}},
    "goal": "Bikin post slot {{slot}} sebanyak {{n}}",
    "verify_cmd": "check --slot {{payload.slot}}",
    "role": "buffer-promo",
    "max_iterations": 4,
    "idempotency_key": "promo:{{slot}}",
}


@pytest.fixture
def cfg(tmp_path):
    c = config.load("/nonexistent")
    c["paths"]["scratch"] = str(tmp_path / "ws")
    c["tasks"] = {"promo-post": dict(TASK, workdir=str(tmp_path))}
    return c


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


# ---- render ----

def test_render_dua_bentuk_variabel():
    assert tasks.render("a {{x}} b {{payload.x}}", {"x": 1}) == "a 1 b 1"


def test_render_variabel_hilang_error():
    with pytest.raises(tasks.TaskError, match="slot"):
        tasks.render("slot {{slot}}", {"lain": 1})


def test_render_tanpa_variabel_apa_adanya():
    assert tasks.render("npm run build", {}) == "npm run build"


# ---- payload ----

def test_prepare_payload_isi_defaults():
    assert tasks.prepare_payload(TASK, {"slot": "pagi"}) == {"slot": "pagi", "n": 10}


def test_prepare_payload_override_default():
    assert tasks.prepare_payload(TASK, {"slot": "sore", "n": 3})["n"] == 3


def test_prepare_payload_required_kurang():
    with pytest.raises(tasks.TaskError, match="slot"):
        tasks.prepare_payload(TASK, {})


# ---- resolve ----

def test_resolve_render_semua_field(cfg):
    out = tasks.resolve(cfg, "promo-post", {"slot": "pagi"})
    assert out["goal"] == "Bikin post slot pagi sebanyak 10"
    assert out["verify_cmd"] == "check --slot pagi"
    assert out["idempotency_key"] == "promo:pagi"
    assert out["role"] == "buffer-promo"
    assert out["max_iterations"] == 4
    assert out["payload"] == {"slot": "pagi", "n": 10}
    assert out["task_id"] == "promo-post"


def test_resolve_default_dari_config_loops(cfg):
    out = tasks.resolve(cfg, "promo-post", {"slot": "pagi"})
    assert out["max_cost_usd"] == cfg["loops"]["max_cost_usd"]  # task nggak nyetel


def test_resolve_override_boleh_terbatas(cfg):
    out = tasks.resolve(cfg, "promo-post", {"slot": "pagi"},
                        overrides={"max_iterations": 9, "model": None})
    assert out["max_iterations"] == 9
    with pytest.raises(tasks.TaskError, match="role"):
        tasks.resolve(cfg, "promo-post", {"slot": "pagi"}, overrides={"role": "lain"})


def test_resolve_task_nggak_ada(cfg):
    with pytest.raises(tasks.TaskError, match="registry"):
        tasks.resolve(cfg, "hantu", {})


# ---- trigger ----

def test_trigger_bikin_run(cfg, store):
    out = tasks.trigger(store, cfg, "promo-post", {"slot": "pagi"})
    run = store.get_run(out["run_id"])
    assert out["deduped"] is False
    assert run["task_id"] == "promo-post"
    assert run["payload"] == {"slot": "pagi", "n": 10}      # JSON di-decode Store
    assert run["fingerprint"] == "promo:pagi"
    assert run["goal"] == "Bikin post slot pagi sebanyak 10"
    assert run["status"] == "queued"


def test_trigger_dedup_per_idempotency_key(cfg, store):
    first = tasks.trigger(store, cfg, "promo-post", {"slot": "pagi"})
    again = tasks.trigger(store, cfg, "promo-post", {"slot": "pagi"})
    assert again["deduped"] is True and again["run_id"] == first["run_id"]
    # payload beda → key beda → run baru
    other = tasks.trigger(store, cfg, "promo-post", {"slot": "sore"})
    assert other["deduped"] is False and other["run_id"] != first["run_id"]


def test_trigger_dedup_lepas_setelah_run_selesai(cfg, store):
    first = tasks.trigger(store, cfg, "promo-post", {"slot": "pagi"})
    store.finish(first["run_id"], "succeeded")
    again = tasks.trigger(store, cfg, "promo-post", {"slot": "pagi"})
    assert again["deduped"] is False


def test_trigger_idempotency_key_eksplisit_menang(cfg, store):
    out = tasks.trigger(store, cfg, "promo-post", {"slot": "pagi"},
                        idempotency_key="manual:1")
    assert store.get_run(out["run_id"])["fingerprint"] == "manual:1"


def test_trigger_tanpa_workdir_bikin_workspace(cfg, store, tmp_path):
    cfg["tasks"]["adhoc"] = {"goal": "kerja", "verify_cmd": "exit 0"}
    out = tasks.trigger(store, cfg, "adhoc")
    assert out["workdir"].startswith(str(tmp_path / "ws"))


def test_trigger_workdir_nggak_ada_error(cfg, store):
    cfg["tasks"]["promo-post"]["workdir"] = "/nggak/ada/direktori"
    with pytest.raises(tasks.TaskError, match="workdir"):
        tasks.trigger(store, cfg, "promo-post", {"slot": "pagi"})


# ---- registry ----

def test_load_registry_dari_config(tmp_path):
    cfg = config.load("/nonexistent")
    cfg["tasks"] = {"a": {"goal": "g", "verify_cmd": "v"}}
    cfg["paths"]["tasks"] = str(tmp_path / "kosong")
    assert list(tasks.load_registry(cfg)) == ["a"]


def test_load_registry_file_nimpa_config(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "a.yaml").write_text("goal: dari file\nverify_cmd: v\n")
    (tasks_dir / "catatan.md").write_text("bukan task")
    cfg = config.load("/nonexistent")
    cfg["tasks"] = {"a": {"goal": "dari config", "verify_cmd": "v"}}
    cfg["paths"]["tasks"] = str(tasks_dir)
    registry = tasks.load_registry(cfg)
    assert registry["a"]["goal"] == "dari file"
    assert "catatan" not in registry


def test_load_registry_skip_spec_rusak(tmp_path, caplog):
    cfg = config.load("/nonexistent")
    cfg["paths"]["tasks"] = str(tmp_path / "kosong")
    cfg["tasks"] = {
        "ok": {"goal": "g", "verify_cmd": "v"},
        "tanpa-verify": {"goal": "g"},
        "payload-aneh": {"goal": "g", "verify_cmd": "v", "payload": "bukan mapping"},
    }
    registry = tasks.load_registry(cfg)
    assert list(registry) == ["ok"]


# ---- hot reload ----
# Nambah task nggak boleh butuh restart server (lihat tasks.refresh).

@pytest.fixture
def hot(tmp_path):
    """cfg dengan direktori tasks/ kosong, registry udah ke-load sekali."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    cfg = config.load("/nonexistent")
    cfg["tasks"] = {"lama": {"goal": "g", "verify_cmd": "v"}}
    cfg["paths"]["tasks"] = str(tasks_dir)
    cfg["tasks"] = tasks.load_registry(cfg)
    tasks.refresh(cfg)                    # panggilan pertama: cuma nyatet stamp
    return cfg, tasks_dir


def test_refresh_tanpa_perubahan_nggak_ngapa_ngapain(hot):
    cfg, _ = hot
    assert tasks.refresh(cfg) is False
    assert list(cfg["tasks"]) == ["lama"]


def test_refresh_nangkep_task_file_baru(hot):
    cfg, tasks_dir = hot
    (tasks_dir / "baru.yaml").write_text("goal: g\nverify_cmd: v\n")
    assert tasks.refresh(cfg) is True
    assert sorted(cfg["tasks"]) == ["baru", "lama"]


def test_get_nangkep_task_baru_tanpa_reload_manual(hot):
    """Jalur yang kepake REST/CLI/scheduler: tasks.get() nge-refresh sendiri."""
    cfg, tasks_dir = hot
    with pytest.raises(tasks.TaskError):
        tasks.get(cfg, "baru")
    (tasks_dir / "baru.yaml").write_text("goal: g baru\nverify_cmd: v\n")
    assert tasks.get(cfg, "baru")["goal"] == "g baru"


def test_refresh_nangkep_edit_dan_hapus(hot):
    cfg, tasks_dir = hot
    f = tasks_dir / "baru.yaml"
    f.write_text("goal: versi 1\nverify_cmd: v\n")
    tasks.refresh(cfg)
    f.write_text("goal: versi 2\nverify_cmd: v\n")
    assert tasks.refresh(cfg) is True
    assert cfg["tasks"]["baru"]["goal"] == "versi 2"
    f.unlink()
    assert tasks.refresh(cfg) is True
    assert list(cfg["tasks"]) == ["lama"]


def test_refresh_pertahanin_task_dari_config(hot):
    """Reload nggak boleh ngilangin task yang didefinisiin di config.yaml."""
    cfg, tasks_dir = hot
    (tasks_dir / "baru.yaml").write_text("goal: g\nverify_cmd: v\n")
    tasks.refresh(cfg)
    assert "lama" in cfg["tasks"]


def test_refresh_spec_rusak_nggak_ngerusak_registry(hot, caplog):
    cfg, tasks_dir = hot
    (tasks_dir / "rusak.yaml").write_text("goal: tanpa verify_cmd\n")
    tasks.refresh(cfg)
    assert list(cfg["tasks"]) == ["lama"]


def test_refresh_baca_ulang_tasks_dari_file_config(tmp_path):
    """Kalau paths.config nunjuk file beneran, `tasks:` di situ ikut hot-reload."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("tasks:\n  a:\n    goal: versi 1\n    verify_cmd: v\n")
    cfg = config.load(str(cfg_file))
    cfg["paths"]["tasks"] = str(tmp_path / "kosong")
    cfg["tasks"] = tasks.load_registry(cfg)
    tasks.refresh(cfg)
    assert cfg["tasks"]["a"]["goal"] == "versi 1"

    cfg_file.write_text("tasks:\n  a:\n    goal: versi 2\n    verify_cmd: v\n"
                        "  b:\n    goal: g\n    verify_cmd: v\n")
    assert tasks.refresh(cfg) is True
    assert cfg["tasks"]["a"]["goal"] == "versi 2"
    assert sorted(cfg["tasks"]) == ["a", "b"]


def test_summary_ringkas(cfg):
    s = tasks.summary("promo-post", cfg["tasks"]["promo-post"])
    assert s["id"] == "promo-post" and s["required"] == ["slot"]
    assert s["defaults"] == {"n": 10} and s["has_gate"] is False
