"""Shared pytest fixtures.

Every test gets its own temporary data directory, so tests never touch data/ and
never share SQLite files with each other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recap.core.config import Settings, load_settings
from tests.helpers import OPERATOR_TOKEN


@pytest.fixture(autouse=True)
def private_usage_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own LLM usage ledger, never the user's real one (~/.local/state)."""
    path = tmp_path / "llm-usage.db"
    monkeypatch.setenv("RECAP_USAGE_LEDGER", str(path))
    return path


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Real configs/ settings, but with data written under a temp dir and zero latency.

    For the API: FastAPI's TestClient addresses requests to the host "testserver", so that
    name is allowed next to the loopback names, and a known operator token is configured
    (tests/helpers.py: OPERATOR_HEADERS). Tests that send no token still get 401.
    """
    monkeypatch.delenv("RECAP_PROVIDER", raising=False)
    monkeypatch.setenv("RECAP_TOOL_LATENCY_MS", "0")
    monkeypatch.setenv("RECAP_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver")
    monkeypatch.setenv("RECAP_OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.delenv("RECAP_OPERATOR_NAME", raising=False)
    loaded = load_settings(data_dir=tmp_path / "data")
    options = dict(loaded.environment.options, write_latency_ms=0)
    return loaded.model_copy(
        update={"environment": loaded.environment.model_copy(update={"options": options})}
    )
