import os
import sys
import tempfile
from pathlib import Path

# Configure before anything imports config.
_TMP = tempfile.mkdtemp(prefix="corkboard-tests-")
os.environ.update({
    "CORKBOARD_DATA_DIR": _TMP,
    "PRINTER_DEVICE": "dry-run",
    "CORKBOARD_TIMEZONE": "America/New_York",
    "CORKBOARD_SECRET_KEY": "test",
    "MCP_SECRET": "",
    "GITHUB_TOKEN": "",
    "GITHUB_WEBHOOK_SECRET": "",
    "GITHUB_USERNAME": "",
    "CORKBOARD_WEB_USERNAME": "",
    "CORKBOARD_WEB_PASSWORD": "",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import config  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "corkboard.db")
    monkeypatch.setattr(config, "PRINTED_DIR", tmp_path / "printed")
    monkeypatch.setattr(config, "PRINTER_DRY_RUN", True)
    monkeypatch.setattr(config, "PRINT_ON_CREATE", True)
    from app.db import init_db
    from app import github

    github.my_login.cache_clear()
    init_db()
    yield tmp_path


@pytest.fixture
def today():
    from app.models import today as _today

    return _today()
