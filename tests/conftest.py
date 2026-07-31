import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import config  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_paths(tmp_path, monkeypatch):
    """Tests must NEVER touch the real directories in the repo.

    `paths.workspaces` is the tenant config directory now — left at its default
    ("workspaces"), every create_app() in a test would load the production
    workspaces (onecookie/jetorbit): the real scheduler & watchdog spin up,
    webhook projects get mixed up, and the tests turn slow + lying.
    `paths.scratch` is pointed at tmp too so we don't leave workdir junk behind.
    """
    monkeypatch.setitem(config.DEFAULTS["paths"], "workspaces",
                        str(tmp_path / "wscfg"))
    monkeypatch.setitem(config.DEFAULTS["paths"], "scratch",
                        str(tmp_path / "scratch"))
