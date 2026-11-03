"""The SREGym adapter (integrations/sregym, M5 W5) and its target profile (sregym-hotel).

SREGymEnvironment is RECAP's Kubernetes adapter in the mode a benchmark's own cluster needs
(ADR 0031): SREGym's harness owns the fault, a release is an image tag, and there is no probe
or metric. These tests cover what is specific to it, offline: its self-description, the profile
and scenario as RECAP_TARGET=sregym-hotel loads them, and that nothing about the problem's
answer can reach an agent. The adapter's behaviour over a fake cluster is
tests/unit/test_kind_external.py; the live cluster is infra/sregym/ and docs/m5-progress.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from integrations.sregym import SREGymEnvironment
from recap.core.config import REPO_ROOT, load_settings
from recap.core.errors import ConfigError
from recap.core.interfaces import TargetEnvironment
from recap.environments.kubernetes import KubernetesEnvironment
from recap.environments.registry import create_environment
from recap.runtime import ENVIRONMENT_ADAPTERS, LIVE_ADAPTERS
from tests.unit.test_kind_external import options as fake_options

SCENARIO = "sregym-misconfig-app-hotel-res"
# What the problem's answer and SREGym's own wording are: never in what an agent can read.
ANSWER = ("geo", "27777", "misconfig", "sha-d2c036bc3d11", "20260912.1", "mongodb")


@pytest.fixture
def profile(tmp_path: Path) -> Any:
    return load_settings(data_dir=tmp_path / "data", target="sregym-hotel")


def test_it_is_the_kubernetes_adapter_in_external_harness_mode(tmp_path: Path) -> None:
    env = SREGymEnvironment("run-sregym01", **fake_options(tmp_path, harness="external"))
    assert isinstance(env, KubernetesEnvironment) and isinstance(env, TargetEnvironment)
    assert (env.kind, env.options.harness, env.options.release_format) == (
        "sregym",
        "external",
        "tag",
    )
    with pytest.raises(ConfigError, match="only in external-harness mode"):
        SREGymEnvironment("run-sregym01", **fake_options(tmp_path, harness="recap"))


def test_describe_says_what_it_is_and_nothing_about_the_problem(tmp_path: Path) -> None:
    info = SREGymEnvironment("run-sregym01", **fake_options(tmp_path)).describe()
    assert (info.kind, info.is_simulated, info.supported_actions) == (
        "sregym",
        False,
        ["rollback", "restart"],
    )
    assert info.name == "SREGym on Kind cluster recap-m5 (offline fake)"
    assert info.description.startswith("OFFLINE FAKE of a SREGym target")
    assert "SREGym's own oracle grades the result" in info.description
    text = f"{info.name} {info.description}".lower()
    assert not any(word in text for word in ANSWER)
    assert "/" not in info.description.replace("rollback to a named revision", "")  # no path


def test_the_profile_selects_the_sregym_adapter_with_its_own_allowlists(profile: Any) -> None:
    assert profile.environment.kind == "sregym"
    options = profile.environment.options
    services = options["services"]
    assert len(services) == 19 and options["namespace"] == "hotel-reservation"
    assert (options["harness"], options["release_format"], options["prometheus_url"]) == (
        "external",
        "tag",
        None,
    )
    policies = profile.policies
    assert policies.allowed_namespaces == ["hotel-reservation"]
    assert sorted(policies.allowed_services) == sorted(services)
    assert policies.tools["execute_rollback"].args["to_version"].type == "release"
    assert policies.tools["delete_namespace"].risk.value == "destructive"
    assert all(
        set(called) <= set(services) for called in options["dependencies"].values()
    )  # the documented call graph stays inside the allowlist
    assert profile.budget.default_budget.max_elapsed_seconds == 3600


def test_the_profile_builds_offline_and_lists_its_scenario(profile: Any) -> None:
    env = create_environment(profile.environment, "describe")  # never contacts the cluster
    assert isinstance(env, SREGymEnvironment)
    [scenario] = env.list_scenarios()
    assert scenario.id == SCENARIO and scenario.partition.value == "test"  # held out
    assert scenario.expected["fix"] == "execute_rollback"  # evaluation only
    info = env.describe()
    assert info.namespace == "hotel-reservation" and info.name == "SREGym on Kind cluster kind"


def test_nothing_an_agent_reads_names_the_answer(profile: Any) -> None:
    """The scenario's labels are evaluation-only; the agents see the measured alert, the
    environment kind and their tools' results. Neither the profile's description nor its
    runbooks (checked by tests/unit/test_k8s_runbooks.py) holds the answer."""
    env = create_environment(profile.environment, "describe")
    text = f"{env.describe().description} {env.describe().name}".lower()
    assert not any(word in text for word in ANSWER)
    [scenario] = env.list_scenarios()
    assert scenario.alert_service == "frontend"  # the alert rule's preferred name: not the cause


def test_get_system_reports_the_adapter_as_implemented_and_live() -> None:
    adapter, implemented, detail = ENVIRONMENT_ADAPTERS["sregym"]
    assert (adapter, implemented) == ("SREGymEnvironment", True)
    assert "sregym" in LIVE_ADAPTERS and "RECAP_TARGET=sregym-hotel" in detail
    assert (REPO_ROOT / "configs" / "targets" / "sregym-hotel" / "environment.yaml").is_file()
