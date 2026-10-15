"""Shared pytest fixtures.

Every test gets its own temporary data directory, so tests never touch data/ and
never share SQLite files with each other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recap.core.config import Settings, load_settings


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Real configs/ settings, but with data written under a temp dir and zero latency."""
    monkeypatch.delenv("RECAP_PROVIDER", raising=False)
    monkeypatch.setenv("RECAP_TOOL_LATENCY_MS", "0")
    loaded = load_settings(data_dir=tmp_path / "data")
    options = dict(loaded.environment.options, write_latency_ms=0)
    return loaded.model_copy(
        update={"environment": loaded.environment.model_copy(update={"options": options})}
    )
