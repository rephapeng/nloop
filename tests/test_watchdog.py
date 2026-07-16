"""Watchdog Sentry: poll → normalize → dedup/cooldown → spawn issue-fix run."""
import asyncio
import json
import time

import httpx
import pytest

from engine import config
from engine.store import Store
from engine.watchdog import Watchdog, _entry_interval, _entry_max_per_tick, _entry_name

ISSUES = [
    {"id": "111", "title": "TypeError: x is not a function",
     "culprit": "app/page.tsx in Home", "permalink": "https://sentry.io/x/111/"},
    {"id": "222", "title": "ReferenceError: y is not defined",
     "culprit": "lib/hpp.ts in num", "permalink": "https://sentry.io/x/222/"},
    {"id": "333", "title": "Error: boom",
     "culprit": "", "permalink": ""},
]


@pytest.fixture
def cfg(tmp_path):
    c = config.load("/nonexistent")
    proj = tmp_path / "proj"
    proj.mkdir()
    c["triggers"]["projects"] = {
        "marginin": {"workdir": str(proj), "verify_cmd": "npm run build",
                     "on_success_cmd": "echo deploy"},
    }
    c["triggers"]["sentry"] = {"resolve": False, "url": "https://sentry.example.com"}
    c["watchdog"] = {"enabled": True, "interval": "5m", "cooldown": "24h",
                     "max_per_tick": 2, "organization": "metatech",
                     "projects": {"marginin-js": "marginin"},
                     "query": "is:unresolved"}
    return c


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "wd.db"))


def make_wd(store, cfg, issues=ISSUES, status=200, capture=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        return httpx.Response(status, json=issues)
    return Watchdog(store, cfg, transport=httpx.MockTransport(handler))


def tick(wd):
    return asyncio.run(wd.tick())


def test_tick_spawns_runs_with_issue_pipeline(store, cfg, monkeypatch):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    reqs = []
    spawned = tick(make_wd(store, cfg, capture=reqs))
    assert len(spawned) == 2                                  # max_per_tick cap

    r = store.get_run(spawned[0])
    assert r["fingerprint"] == "sentry:111"
    assert "TypeError" in r["goal"] and "REPRO" in r["goal"]  # jalur issue-fix penuh
    assert r["verify_cmd"].startswith("sh .nloop/repro/sentry-111.sh")
    assert r["on_success_cmd"] == "echo deploy"

    req = reqs[0]
    assert "api/0/projects/metatech/marginin-js/issues" in str(req.url)
    assert "is%3Aunresolved" in str(req.url) or "is:unresolved" in str(req.url)
    assert req.headers["Authorization"] == "Bearer tok"


def test_tick_dedups_active_fingerprint(store, cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    store.create_run("g", "exit 0", str(tmp_path), fingerprint="sentry:111")  # queued
    spawned = tick(make_wd(store, cfg))
    fps = [store.get_run(r)["fingerprint"] for r in spawned]
    assert "sentry:111" not in fps                            # aktif → skip
    assert "sentry:222" in fps


def test_tick_respects_cooldown(store, cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    rid = store.create_run("g", "exit 0", str(tmp_path), fingerprint="sentry:111")
    store.finish(rid, "failed")                               # baru aja gagal
    spawned = tick(make_wd(store, cfg))
    fps = [store.get_run(r)["fingerprint"] for r in spawned]
    assert "sentry:111" not in fps                            # cooldown 24h

    # run lama (> cooldown) → boleh dicoba lagi
    store.db.execute("UPDATE runs SET ended_at=? WHERE id=?",
                     (time.time() - 100_000, rid))
    store.db.commit()
    spawned2 = tick(make_wd(store, cfg))
    fps2 = [store.get_run(r)["fingerprint"] for r in spawned2]
    assert "sentry:111" in fps2


def test_tick_no_token_skips(store, cfg, monkeypatch):
    monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)
    assert tick(make_wd(store, cfg)) == []
    assert store.list_runs() == []


def test_tick_api_error_no_crash(store, cfg, monkeypatch):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    assert tick(make_wd(store, cfg, status=500)) == []


def test_tick_unknown_project_mapping_skipped(store, cfg, monkeypatch):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    cfg["watchdog"]["projects"] = {"marginin-js": "ghost"}
    assert tick(make_wd(store, cfg)) == []


def test_normalize_matches_webhook_shape():
    issue = Watchdog._normalize(ISSUES[0])
    assert issue == {"fingerprint": "sentry:111",
                     "title": "TypeError: x is not a function",
                     "url": "https://sentry.io/x/111/",
                     "detail": "app/page.tsx in Home"}
    empty = Watchdog._normalize(ISSUES[2])
    assert empty["url"] == "" and empty["detail"] == ""


# ---- per-project interval override (beda-beda per app) ----

def test_entry_helpers_support_string_and_dict_form():
    assert _entry_name("marginin") == "marginin"
    assert _entry_name({"name": "marginin", "interval": "30m"}) == "marginin"

    assert _entry_interval("marginin", default="1h") == "1h"           # string form -> default
    assert _entry_interval({"name": "marginin"}, default="1h") == "1h"  # dict tanpa override
    assert _entry_interval({"name": "marginin", "interval": "30m"}, default="1h") == "30m"

    assert _entry_max_per_tick("marginin", default=2) == 2
    assert _entry_max_per_tick({"name": "marginin", "max_per_tick": 1}, default=2) == 1


def test_status_exposes_per_project_interval_override(store, cfg):
    cfg["watchdog"]["interval"] = "1h"
    cfg["watchdog"]["projects"] = {
        "marginin-js": {"name": "marginin", "interval": "30m"},
        "onecookie-py": "onecookie",           # bentuk pendek -> pakai default
    }
    st = Watchdog(store, cfg).status()
    assert st["project_intervals"] == {"marginin-js": "30m", "onecookie-py": "1h"}
    assert st["projects"] == {"marginin-js": "marginin", "onecookie-py": "onecookie"}


def test_tick_project_dict_entry_respects_own_max_per_tick(store, cfg, monkeypatch):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    entry = {"name": "marginin", "interval": "30m", "max_per_tick": 1}
    cfg["watchdog"]["projects"] = {"marginin-js": entry}
    wd = make_wd(store, cfg)

    spawned = asyncio.run(wd._tick_project("marginin-js", entry))
    assert len(spawned) == 1                              # override, bukan max_per_tick global (2)
    assert wd.project_status["marginin-js"]["last_checked"] == 3
    assert wd.project_status["marginin-js"]["last_spawned"] == spawned


def test_run_project_ticks_on_its_own_independent_interval(store, cfg, monkeypatch):
    calls = []

    async def fake_tick(slug, entry):
        calls.append(slug)
        return []

    wd = make_wd(store, cfg)
    monkeypatch.setattr(wd, "_tick_project", fake_tick)

    async def run():
        task = asyncio.create_task(wd._run_project("marginin-js", "marginin", 0.03))
        await asyncio.sleep(0.12)
        await wd.stop()
        await task

    asyncio.run(run())
    assert len(calls) >= 2                                # tick berulang di interval sendiri