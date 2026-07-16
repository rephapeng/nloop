"""Roles + grounding (context_cmd) → system prompt (port run_claude.sh dtc)."""
import asyncio

import pytest

from engine import config, grounding, loop
from engine.claude_cli import ClaudeResult
from engine.store import Store


@pytest.fixture
def cfg(tmp_path):
    c = config.load("/nonexistent")
    c["paths"]["roles"] = str(tmp_path / "roles")
    (tmp_path / "roles").mkdir()
    return c


def write_role(cfg, name, text):
    import os
    with open(os.path.join(cfg["paths"]["roles"], f"{name}.md"), "w") as f:
        f.write(text)


def build(cfg, **kw):
    return asyncio.run(grounding.build_system_prompt(cfg, **kw))


def test_role_missing_raises(cfg):
    with pytest.raises(ValueError, match="role 'ghost'"):
        grounding.role_prompt(cfg, "ghost")


def test_empty_sources_give_none(cfg):
    assert build(cfg) is None


def test_common_role_and_context_combined(cfg, tmp_path):
    write_role(cfg, "common", "ATURAN BERSAMA")
    write_role(cfg, "writer", "kamu penulis")
    sp = build(cfg, role="writer", context_cmd="echo GROUNDING-SEGAR",
               workdir=str(tmp_path))
    assert sp.index("ATURAN BERSAMA") < sp.index("GROUNDING-SEGAR") < sp.index("kamu penulis")
    assert "===== INJECTED GROUNDING (context_cmd) =====" in sp
    assert "===== ROLE =====" in sp


def test_context_cmd_failure_not_fatal(cfg, tmp_path):
    sp = build(cfg, context_cmd="echo duar; exit 3", workdir=str(tmp_path))
    assert "[context_cmd exit 3]" in sp and "duar" in sp


def test_context_output_capped(cfg, tmp_path):
    sp = build(cfg, context_cmd="yes x | head -c 50000", workdir=str(tmp_path))
    assert len(sp) < 30000
    assert "grounding dipotong di cap" in sp


def test_loop_injects_system_prompt(monkeypatch, cfg, tmp_path):
    """Run dengan role+context_cmd → claude_cli.run dapet system_prompt gabungan."""
    write_role(cfg, "fixer", "PERSONA-FIXER")
    captured = {}

    async def fake_run(prompt, *, cwd, resume=None, **kwargs):
        captured["system_prompt"] = kwargs.get("system_prompt")
        return ClaudeResult(ok=True, subtype="success", result_text="ok",
                            session_id="s", cost_usd=0.01, num_turns=1)

    monkeypatch.setattr(loop.claude_cli, "run", fake_run)
    store = Store(str(tmp_path / "t.db"))
    wd = tmp_path / "ws"
    wd.mkdir()
    run_id = store.create_run("g", "test -f x", str(wd), max_iterations=1,
                              role="fixer", context_cmd="echo CTX-123")
    asyncio.run(loop.run_loop(run_id, store, cfg))
    assert "PERSONA-FIXER" in captured["system_prompt"]
    assert "CTX-123" in captured["system_prompt"]
