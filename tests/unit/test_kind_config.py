"""The kind-demo target profile and RECAP_TARGET (recap/core/config.py, configs/targets/).

A target profile replaces the files that describe one target (environment, policies, budget)
and nothing else; without RECAP_TARGET nothing changes, so tests, demos and CI stay on the
simulator. The run's config hash covers the files actually read. GET /system reports the
Kind adapter as implemented, and as operational only while it is the configured one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recap.core.config import (
    CONFIG_FILES,
    REPO_ROOT,
    Settings,
    config_files,
    load_settings,
)
from recap.core.errors import ConfigError
from recap.core.ids import sha256_hex
from recap.core.models import ToolRisk
from recap.environments.kubernetes import KubernetesEnvironment
from recap.environments.registry import create_environment
from recap.runtime import STDIO_TRANSPORT, Runtime

CONFIGS = REPO_ROOT / "configs"


@pytest.fixture
def kind_settings(settings: Settings, tmp_path: Path) -> Settings:
    """The kind-demo profile, loaded like `settings` (same environment variables, tmp data)."""
    return load_settings(data_dir=tmp_path / "kind-data", target="kind-demo")


def test_without_a_target_nothing_changes(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RECAP_TARGET", raising=False)
    default = load_settings(data_dir=settings.paths.data_dir)

    assert default.target is None and default.environment.kind == "simulated"
    texts = "".join((CONFIGS / name).read_text(encoding="utf-8") for name in CONFIG_FILES)
    assert default.config_hash == sha256_hex(texts)[:16]  # the hash of before profiles existed


def test_the_profile_replaces_environment_policies_and_budget_only(
    settings: Settings, kind_settings: Settings
) -> None:
    files = config_files(CONFIGS, "kind-demo")
    profile = CONFIGS / "targets" / "kind-demo"
    assert {name for name, path in files.items() if path.parent == profile} == {
        "environment.yaml",
        "policies.yaml",
        "budget.yaml",
    }
    assert kind_settings.target == "kind-demo"
    assert kind_settings.agents == settings.agents and kind_settings.providers == settings.providers
    texts = "".join(files[name].read_text(encoding="utf-8") for name in CONFIG_FILES)
    texts += (profile / "knowledge.yaml").read_text(encoding="utf-8")  # the profile's runbooks
    assert kind_settings.config_hash == sha256_hex(texts)[:16] != settings.config_hash


def test_the_kind_policy_allows_one_namespace_and_two_services_with_the_same_rules(
    settings: Settings, kind_settings: Settings
) -> None:
    kind, default = kind_settings.policies, settings.policies

    assert kind.version == "policy-kind-v1"
    assert kind.allowed_namespaces == ["recap-demo"]
    assert kind.allowed_services == ["frontend", "backend"]
    assert kind.tools == default.tools  # the same tiers and argument rules
    assert kind.tools["delete_namespace"].risk == ToolRisk.DESTRUCTIVE
    assert (
        kind.remediation_tools
        == default.remediation_tools
        == ["execute_rollback", "restart_service"]
    )
    assert (kind.limits, kind.oracle) == (default.limits, default.oracle)


def test_the_kind_budget_covers_a_live_stabilization_window(
    settings: Settings, kind_settings: Settings
) -> None:
    kind = kind_settings.budget
    assert kind.default_budget.max_elapsed_seconds == 3600
    assert kind.default_budget.model_copy(update={"max_elapsed_seconds": 120}) == (
        settings.budget.default_budget
    )
    assert (kind.routing, kind.retries, kind.approvals) == (
        settings.budget.routing,
        settings.budget.retries,
        settings.budget.approvals,
    )


def test_the_kind_environment_reads_its_own_scenarios_and_needs_no_cluster_to_build(
    kind_settings: Settings,
) -> None:
    environment = kind_settings.environment
    assert environment.kind == "kind"
    assert Path(environment.options["scenarios_dir"]) == REPO_ROOT / "fixtures" / "kind-scenarios"
    assert environment.options["state_dir"] == str(kind_settings.paths.sandbox_dir)
    assert environment.options["credentials_dir"] == "~/.config/recap/kind"  # never a real path

    built = create_environment(environment, "describe")  # what every GET /system does

    assert isinstance(built, KubernetesEnvironment)
    assert built.describe().namespace == "recap-demo"
    assert [s.id for s in built.list_scenarios()][0] == "kind-bad-release"
    assert not kind_settings.paths.sandbox_dir.exists()  # nothing was created


def test_recap_target_picks_the_profile_and_an_empty_argument_keeps_the_default(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECAP_TARGET", "kind-demo")
    assert load_settings(data_dir=settings.paths.data_dir).environment.kind == "kind"
    assert (
        load_settings(data_dir=settings.paths.data_dir, target="").environment.kind == "simulated"
    )


@pytest.mark.parametrize("target", ["nope", "../configs", "kind-demo/../..", "KIND", "a" * 80])
def test_an_unknown_or_path_like_target_is_refused(settings: Settings, target: str) -> None:
    with pytest.raises(ConfigError, match="unknown target profile") as refused:
        load_settings(data_dir=settings.paths.data_dir, target=target)
    assert len(str(refused.value)) < 160


# --- what GET /system says ----------------------------------------------------------------------


def adapters(runtime: Runtime) -> dict[str, dict]:
    return {a["kind"]: a for a in runtime._environment_adapters(len(runtime.list_scenarios()))}


def test_on_the_simulator_kind_is_implemented_but_never_operational(settings: Settings) -> None:
    kind = adapters(Runtime(settings))["kind"]

    assert (kind["adapter"], kind["status"], kind["configured"]) == (
        "KubernetesEnvironment",
        "available",
        False,
    )
    assert kind["operational"] is False and kind["milestone"] is None
    assert "never contacts the cluster" in kind["detail"]


def test_configured_kind_is_the_active_adapter_with_its_own_oracle(kind_settings: Settings) -> None:
    runtime = Runtime(kind_settings)
    found = adapters(runtime)
    summary = runtime.system_summary()
    components = {c["id"]: c for c in summary["components"]}

    assert (found["kind"]["status"], found["kind"]["operational"]) == ("active", True)
    assert found["simulated"]["status"] == "available"
    # SREGym's adapter is built too (M5 W5), but only the configured live adapter is operational.
    assert (found["sregym"]["status"], found["sregym"]["operational"]) == ("available", False)
    assert summary["oracle"] == "kind-health-oracle" == components["oracle"]["implementation"]
    assert summary["tool_transport"] == STDIO_TRANSPORT
    assert summary["environment"]["is_simulated"] is False
    assert components["environment"]["status"] == "active"
    assert "NOT simulated" in components["environment"]["detail"]
    assert summary["versions"]["policy_version"] == "policy-kind-v1"


def test_the_kind_profile_reads_its_kubernetes_runbooks_and_the_default_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RECAP_KNOWLEDGE_OVERLAYS", raising=False)
    default = load_settings()
    kind = load_settings(target="kind-demo")
    assert default.paths.knowledge_overlays == []
    assert [p.name for p in kind.paths.knowledge_overlays] == ["kubernetes"]
    assert kind.config_hash != default.config_hash  # the profile's knowledge file is hashed
    # Named by the profile and by RECAP_KNOWLEDGE_OVERLAYS: read once (the index refuses twice).
    monkeypatch.setenv("RECAP_KNOWLEDGE_OVERLAYS", "fixtures/knowledge-overlays/kubernetes")
    assert len(load_settings(target="kind-demo").paths.knowledge_overlays) == 1
