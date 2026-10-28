from __future__ import annotations

import pytest

from clipforge import db
from clipforge.settings import get_settings


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test gets its own data dir and SQLite database."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("FAKE_MODELS", "false")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    db.reset_caches()
    yield tmp_path
    db.reset_caches()
    get_settings.cache_clear()
