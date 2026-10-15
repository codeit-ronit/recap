"""Tests for loading configs/*.yaml into typed settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from recap.core.config import Settings, _check_agents_are_read_only, load_settings
from recap.core.errors import ConfigError
from recap.core.models import AgentRole, ToolRisk


def test_settings_load_from_repo_configs(settings: Settings) -> None:
    assert settings.environment.kind == "simulated"
    assert settings.providers.default == "deterministic"
    assert settings.providers.providers["deterministic"].kind == "scripted"
    assert set(settings.agents) >= {AgentRole.INVESTIGATOR, AgentRole.TELEMETRY}
    assert len(settings.config_hash) == 16


def test_paths_are_absolute_for_the_mcp_subprocess(settings: Settings) -> None:
    assert Path(settings.environment.options["scenarios_dir"]).is_absolute()
    assert Path(settings.environment.options["state_dir"]).is_absolute()


def test_every_worker_tool_is_read_only(settings: Settings) -> None:
    for spec in settings.agents.values():
        for tool in spec.tools:
            assert settings.policies.tools[tool].risk == ToolRisk.READ


def test_a_worker_with_a_write_tool_is_refused(settings: Settings) -> None:
    agents = dict(settings.agents)
    bad = agents[AgentRole.INVESTIGATOR].model_copy(update={"tools": ["execute_rollback"]})
    agents[AgentRole.INVESTIGATOR] = bad
    with pytest.raises(ConfigError):
        _check_agents_are_read_only(agents, settings.policies)


def test_destructive_tool_is_classified_destructive(settings: Settings) -> None:
    assert settings.policies.tools["delete_namespace"].risk == ToolRisk.DESTRUCTIVE
    assert "delete_namespace" not in settings.policies.remediation_tools


def test_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECAP_TOOL_LATENCY_MS", "7")
    monkeypatch.setenv("RECAP_PROVIDER", "deterministic")
    loaded = load_settings(data_dir=tmp_path)
    assert loaded.environment.options["tool_latency_ms"] == 7
    assert loaded.paths.recap_db.parent == tmp_path


def test_unknown_provider_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECAP_PROVIDER", "does-not-exist")
    with pytest.raises(ConfigError):
        load_settings(data_dir=tmp_path)
