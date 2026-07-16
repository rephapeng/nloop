"""Langkah rilis (on_success_cmd) + auto-resolve Sentry setelah run sukses."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from engine import config, loop, sentry
from engine.claude_cli import ClaudeResult
from engine.store import Store


@pytest.fixture
def cfg():
    c = config.load("/nonexistent")
    c["claude"]["model"] = "fake"
    return c


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


@pytest.fixture
def workdir(tmp_path):
    wd = tmp_path / "ws"
    wd.mkdir()
    return str(wd)


def fake_claude(monkeypatch, fixer=None):
    async def fake_run(prompt, *, cwd, resume=None, **kwargs):
        if fixer:
            fixer(cwd)
        return ClaudeResult(ok=True, subtype="success", result_text="ok",
                            session_id="s", cost_usd=0.01, num_turns=1)
    monkeypatch.setattr(loop.claude_cli, "run", fake_run)


def run(store, cfg, run_id):
    return asyncio.run(loop.run_loop(run_id, store, cfg))


# ---- on_success_cmd ----

def test_postrun_runs_after_success(monkeypatch, store, cfg, workdir):
    fake_claude(monkeypatch)
    run_id = store.create_run("g", "exit 0", workdir,
                              on_success_cmd="echo DEPLOYED > deployed.txt")
    assert run(store, cfg, run_id) == "succeeded"
    assert (Path(workdir) / "deployed.txt").read_text().strip() == "DEPLOYED"
    types = [e["type"] for e in store.events_since(run_id)]
    assert "postrun" in types


def test_postrun_failure_fails_run(monkeypatch, store, cfg, workdir):
    fake_claude(monkeypatch)
    run_id = store.create_run("g", "exit 0", workdir,
                              on_success_cmd="echo push ditolak; exit 5")
    assert run(store, cfg, run_id) == "failed"
    last = store.events_since(run_id)[-1]["payload"]
    assert last["reason"] == "postrun_failed"
    postrun = [e for e in store.events_since(run_id) if e["type"] == "postrun"][0]
    assert postrun["payload"]["ok"] is False
    assert "push ditolak" in postrun["payload"]["output"]


def test_postrun_skipped_when_loop_fails(monkeypatch, store, cfg, workdir):
    fake_claude(monkeypatch)
    run_id = store.create_run("g", "exit 1", workdir, max_iterations=1,
                              on_success_cmd="touch deployed.txt")
    assert run(store, cfg, run_id) == "failed"
    assert not (Path(workdir) / "deployed.txt").exists()   # gagal ≠ deploy


def test_postrun_skipped_when_stopped(monkeypatch, store, cfg, workdir):
    fake_claude(monkeypatch)
    run_id = store.create_run("g", "exit 1", workdir,
                              on_success_cmd="touch deployed.txt")
    store.request_stop(run_id)
    assert run(store, cfg, run_id) == "stopped"
    assert not (Path(workdir) / "deployed.txt").exists()


# ---- sentry resolve ----

def resolve(fingerprint, cfg, transport=None):
    return asyncio.run(sentry.resolve_issue(fingerprint, cfg, transport=transport))


def test_resolve_noop_when_disabled_or_not_sentry(cfg, monkeypatch):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    assert resolve("sentry:123", cfg) is None                 # resolve: false (default)
    cfg["triggers"]["sentry"]["resolve"] = True
    assert resolve("schedule:harian", cfg) is None            # bukan issue sentry
    assert resolve(None, cfg) is None


def test_resolve_warns_without_token(cfg, monkeypatch):
    monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)
    cfg["triggers"]["sentry"]["resolve"] = True
    level, msg = resolve("sentry:123", cfg)
    assert level == "warn" and "SENTRY_AUTH_TOKEN" in msg


def test_resolve_puts_resolved_status(cfg, monkeypatch):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok-rahasia")
    cfg["triggers"]["sentry"] = {"resolve": True, "url": "https://sentry.example.com"}
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "resolved"})

    level, msg = resolve("sentry:6120345678", cfg,
                         transport=httpx.MockTransport(handler))
    assert level == "info" and "resolved" in msg
    assert seen["method"] == "PUT"
    assert seen["url"] == "https://sentry.example.com/api/0/issues/6120345678/"
    assert seen["auth"] == "Bearer tok-rahasia"
    assert seen["body"] == {"status": "resolved"}


def test_resolve_api_error_is_warning_not_crash(cfg, monkeypatch):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    cfg["triggers"]["sentry"]["resolve"] = True
    transport = httpx.MockTransport(lambda r: httpx.Response(403, text="no scope"))
    level, msg = resolve("sentry:1", cfg, transport=transport)
    assert level == "warn" and "403" in msg


def test_loop_emits_resolve_log(monkeypatch, store, cfg, workdir):
    """E2E kecil: run sukses dengan fingerprint sentry → event log resolve muncul."""
    fake_claude(monkeypatch)
    cfg["triggers"]["sentry"]["resolve"] = True
    monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)    # → jalur warning, no network
    run_id = store.create_run("g", "exit 0", workdir, fingerprint="sentry:99")
    assert run(store, cfg, run_id) == "succeeded"
    logs = [e["payload"] for e in store.events_since(run_id) if e["type"] == "log"]
    assert any("SENTRY_AUTH_TOKEN" in l.get("msg", "") for l in logs)