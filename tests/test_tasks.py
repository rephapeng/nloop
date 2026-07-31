"""Task registry: load, template rendering, payload validation, trigger + dedup."""
import pytest

from engine import config, tasks
from engine.store import Store

TASK = {
    "name": "Promo post",
    "payload": {"required": ["slot"], "defaults": {"n": 10}},
    "goal": "Write {{n}} posts for slot {{slot}}",
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

def test_render_both_variable_forms():
    assert tasks.render("a {{x}} b {{payload.x}}", {"x": 1}) == "a 1 b 1"


def test_render_missing_variable_errors():
    with pytest.raises(tasks.TaskError, match="slot"):
        tasks.render("slot {{slot}}", {"other": 1})


def test_render_without_variables_is_verbatim():
    assert tasks.render("npm run build", {}) == "npm run build"


# ---- payload ----

def test_prepare_payload_fills_defaults():
    assert tasks.prepare_payload(TASK, {"slot": "pagi"}) == {"slot": "pagi", "n": 10}


def test_prepare_payload_overrides_default():
    assert tasks.prepare_payload(TASK, {"slot": "sore", "n": 3})["n"] == 3


def test_prepare_payload_missing_required():
    with pytest.raises(tasks.TaskError, match="slot"):
        tasks.prepare_payload(TASK, {})


# ---- resolve ----

def test_resolve_renders_every_field(cfg):
    out = tasks.resolve(cfg, "promo-post", {"slot": "pagi"})
    assert out["goal"] == "Write 10 posts for slot pagi"
    assert out["verify_cmd"] == "check --slot pagi"
    assert out["idempotency_key"] == "promo:pagi"
    assert out["role"] == "buffer-promo"
    assert out["max_iterations"] == 4
    assert out["payload"] == {"slot": "pagi", "n": 10}
    assert out["task_id"] == "promo-post"


def test_resolve_default_from_config_loops(cfg):
    out = tasks.resolve(cfg, "promo-post", {"slot": "pagi"})
    assert out["max_cost_usd"] == cfg["loops"]["max_cost_usd"]  # task doesn't set it


def test_resolve_override_is_limited(cfg):
    out = tasks.resolve(cfg, "promo-post", {"slot": "pagi"},
                        overrides={"max_iterations": 9, "model": None})
    assert out["max_iterations"] == 9
    with pytest.raises(tasks.TaskError, match="role"):
        tasks.resolve(cfg, "promo-post", {"slot": "pagi"}, overrides={"role": "other"})


def test_resolve_unknown_task(cfg):
    with pytest.raises(tasks.TaskError, match="registry"):
        tasks.resolve(cfg, "ghost", {})


# ---- trigger ----

def test_trigger_creates_run(cfg, store):
    out = tasks.trigger(store, cfg, "promo-post", {"slot": "pagi"})
    run = store.get_run(out["run_id"])
    assert out["deduped"] is False
    assert run["task_id"] == "promo-post"
    assert run["payload"] == {"slot": "pagi", "n": 10}      # JSON decoded by Store
    assert run["fingerprint"] == "promo:pagi"
    assert run["goal"] == "Write 10 posts for slot pagi"
    assert run["status"] == "queued"


def test_trigger_dedup_per_idempotency_key(cfg, store):
    first = tasks.trigger(store, cfg, "promo-post", {"slot": "pagi"})
    again = tasks.trigger(store, cfg, "promo-post", {"slot": "pagi"})
    assert again["deduped"] is True and again["run_id"] == first["run_id"]
    # different payload → different key → new run
    other = tasks.trigger(store, cfg, "promo-post", {"slot": "sore"})
    assert other["deduped"] is False and other["run_id"] != first["run_id"]


def test_trigger_dedup_released_after_run_finishes(cfg, store):
    first = tasks.trigger(store, cfg, "promo-post", {"slot": "pagi"})
    store.finish(first["run_id"], "succeeded")
    again = tasks.trigger(store, cfg, "promo-post", {"slot": "pagi"})
    assert again["deduped"] is False


def test_trigger_explicit_idempotency_key_wins(cfg, store):
    out = tasks.trigger(store, cfg, "promo-post", {"slot": "pagi"},
                        idempotency_key="manual:1")
    assert store.get_run(out["run_id"])["fingerprint"] == "manual:1"


def test_trigger_without_workdir_creates_workspace(cfg, store, tmp_path):
    cfg["tasks"]["adhoc"] = {"goal": "do work", "verify_cmd": "exit 0"}
    out = tasks.trigger(store, cfg, "adhoc")
    assert out["workdir"].startswith(str(tmp_path / "ws"))


def test_trigger_missing_workdir_errors(cfg, store):
    cfg["tasks"]["promo-post"]["workdir"] = "/no/such/directory"
    with pytest.raises(tasks.TaskError, match="workdir"):
        tasks.trigger(store, cfg, "promo-post", {"slot": "pagi"})


# ---- registry ----

def test_load_registry_from_config(tmp_path):
    cfg = config.load("/nonexistent")
    cfg["tasks"] = {"a": {"goal": "g", "verify_cmd": "v"}}
    cfg["paths"]["tasks"] = str(tmp_path / "empty")
    assert list(tasks.load_registry(cfg)) == ["a"]


def test_load_registry_file_beats_config(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "a.yaml").write_text("goal: from file\nverify_cmd: v\n")
    (tasks_dir / "notes.md").write_text("not a task")
    cfg = config.load("/nonexistent")
    cfg["tasks"] = {"a": {"goal": "from config", "verify_cmd": "v"}}
    cfg["paths"]["tasks"] = str(tasks_dir)
    registry = tasks.load_registry(cfg)
    assert registry["a"]["goal"] == "from file"
    assert "notes" not in registry


def test_load_registry_skips_broken_spec(tmp_path, caplog):
    cfg = config.load("/nonexistent")
    cfg["paths"]["tasks"] = str(tmp_path / "empty")
    cfg["tasks"] = {
        "ok": {"goal": "g", "verify_cmd": "v"},
        "no-verify": {"goal": "g"},
        "weird-payload": {"goal": "g", "verify_cmd": "v", "payload": "not a mapping"},
    }
    registry = tasks.load_registry(cfg)
    assert list(registry) == ["ok"]


# ---- hot reload ----
# Adding a task must not require a server restart (see tasks.refresh).

@pytest.fixture
def hot(tmp_path):
    """cfg with an empty tasks/ directory, registry already loaded once."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    cfg = config.load("/nonexistent")
    cfg["tasks"] = {"old": {"goal": "g", "verify_cmd": "v"}}
    cfg["paths"]["tasks"] = str(tasks_dir)
    cfg["tasks"] = tasks.load_registry(cfg)
    tasks.refresh(cfg)                    # first call: only records the stamp
    return cfg, tasks_dir


def test_refresh_without_changes_does_nothing(hot):
    cfg, _ = hot
    assert tasks.refresh(cfg) is False
    assert list(cfg["tasks"]) == ["old"]


def test_refresh_catches_new_task_file(hot):
    cfg, tasks_dir = hot
    (tasks_dir / "new.yaml").write_text("goal: g\nverify_cmd: v\n")
    assert tasks.refresh(cfg) is True
    assert sorted(cfg["tasks"]) == ["new", "old"]


def test_get_catches_new_task_without_manual_reload(hot):
    """The path REST/CLI/scheduler use: tasks.get() refreshes on its own."""
    cfg, tasks_dir = hot
    with pytest.raises(tasks.TaskError):
        tasks.get(cfg, "new")
    (tasks_dir / "new.yaml").write_text("goal: g new\nverify_cmd: v\n")
    assert tasks.get(cfg, "new")["goal"] == "g new"


def test_refresh_catches_edit_and_delete(hot):
    cfg, tasks_dir = hot
    f = tasks_dir / "new.yaml"
    f.write_text("goal: version 1\nverify_cmd: v\n")
    tasks.refresh(cfg)
    f.write_text("goal: version 2\nverify_cmd: v\n")
    assert tasks.refresh(cfg) is True
    assert cfg["tasks"]["new"]["goal"] == "version 2"
    f.unlink()
    assert tasks.refresh(cfg) is True
    assert list(cfg["tasks"]) == ["old"]


def test_refresh_keeps_tasks_from_config(hot):
    """A reload must not lose the tasks defined in config.yaml."""
    cfg, tasks_dir = hot
    (tasks_dir / "new.yaml").write_text("goal: g\nverify_cmd: v\n")
    tasks.refresh(cfg)
    assert "old" in cfg["tasks"]


def test_refresh_broken_spec_doesnt_wreck_registry(hot, caplog):
    cfg, tasks_dir = hot
    (tasks_dir / "broken.yaml").write_text("goal: no verify_cmd\n")
    tasks.refresh(cfg)
    assert list(cfg["tasks"]) == ["old"]


def test_refresh_rereads_tasks_from_config_file(tmp_path):
    """If paths.config points at a real file, its `tasks:` hot-reloads too."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("tasks:\n  a:\n    goal: version 1\n    verify_cmd: v\n")
    cfg = config.load(str(cfg_file))
    cfg["paths"]["tasks"] = str(tmp_path / "empty")
    cfg["tasks"] = tasks.load_registry(cfg)
    tasks.refresh(cfg)
    assert cfg["tasks"]["a"]["goal"] == "version 1"

    cfg_file.write_text("tasks:\n  a:\n    goal: version 2\n    verify_cmd: v\n"
                        "  b:\n    goal: g\n    verify_cmd: v\n")
    assert tasks.refresh(cfg) is True
    assert cfg["tasks"]["a"]["goal"] == "version 2"
    assert sorted(cfg["tasks"]) == ["a", "b"]


def test_summary_is_compact(cfg):
    s = tasks.summary("promo-post", cfg["tasks"]["promo-post"])
    assert s["id"] == "promo-post" and s["required"] == ["slot"]
    assert s["defaults"] == {"n": 10} and s["has_gate"] is False
