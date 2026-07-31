import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import config  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_paths(tmp_path, monkeypatch):
    """Test JANGAN nyentuh direktori beneran di repo.

    `paths.workspaces` sekarang direktori config tenant — kalau dibiarin default
    ("workspaces"), tiap create_app() di test bakal ngeload workspace produksi
    (onecookie/jetorbit): scheduler & watchdog beneran nyala, project webhook
    ketuker, dan test-nya jadi lambat + bohong. `paths.scratch` juga diarahin ke
    tmp biar nggak ninggalin sampah workdir.
    """
    monkeypatch.setitem(config.DEFAULTS["paths"], "workspaces",
                        str(tmp_path / "wscfg"))
    monkeypatch.setitem(config.DEFAULTS["paths"], "scratch",
                        str(tmp_path / "scratch"))
