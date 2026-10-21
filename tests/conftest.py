"""Shared pytest fixtures.

Every test gets its own temporary data directory, so tests never touch data/ and
never share SQLite files with each other.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from recap.core.config import Settings, load_settings
from tests.helpers import OPERATOR_TOKEN


@pytest.fixture(scope="session", autouse=True)
def session_usage_ledger(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A private LLM usage ledger for the whole session, set before any module- or
    session-scoped fixture builds a Runtime: the user's real one (~/.local/state) is never
    opened by the tests."""
    path = tmp_path_factory.mktemp("ledger") / "llm-usage.db"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("RECAP_USAGE_LEDGER", str(path))
        # Even code that ignored the variable would fall back to a temporary state folder.
        patch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("state")))
        yield path


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
