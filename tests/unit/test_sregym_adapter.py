"""Tests for the SREGym adapter stub (integrations/sregym).

The stub must be honest: it says it is not simulated and not integrated, lists no
scenarios, and refuses every operation, so no result can ever be attributed to SREGym by
accident. The recovery oracle must refuse too instead of inventing a health verdict.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import yaml

from integrations.sregym import SREGymEnvironment
from recap.core.config import REPO_ROOT, EnvironmentConfig
from recap.core.errors import EnvironmentNotIntegrated
from recap.core.interfaces import TargetEnvironment
from recap.core.observations import AuthorizedAction
from recap.verification.oracle import EnvironmentHealthOracle

SAMPLE_CONFIG = REPO_ROOT / "integrations" / "sregym" / "sample_config.yaml"

OPERATIONS: dict[str, Callable[[SREGymEnvironment], Awaitable[Any]]] = {
    "reset": lambda env: env.reset("any-problem", 1),
    "observe_services": lambda env: env.observe_services(),
    "observe_health": lambda env: env.observe_health("checkout"),
    "observe_logs": lambda env: env.observe_logs("checkout", 10, 60),
    "observe_metrics": lambda env: env.observe_metrics("checkout", "error_rate", 60),
    "observe_deployments": lambda env: env.observe_deployments(None, 10),
    "observe_events": lambda env: env.observe_events(None, 10),
    "plan_rollback": lambda env: env.plan_rollback("checkout"),
    "execute_authorized": lambda env: env.execute_authorized(
        AuthorizedAction(action="restart", target="checkout")
    ),
    "health_check": lambda env: env.health_check(),
}


def test_describe_says_plainly_that_nothing_is_connected() -> None:
    info = SREGymEnvironment(run_id="run-1").describe()
    assert (info.kind, info.name, info.namespace) == (
        "sregym",
        "SREGym (not integrated)",
        "default",
    )
    assert info.is_simulated is False and info.supported_actions == []
    assert "no connection" in info.description.lower()


def test_namespace_comes_from_the_options() -> None:
    assert SREGymEnvironment(run_id="run-1", namespace="hotel").describe().namespace == "hotel"


def test_binding_an_executor_key_is_refused_too() -> None:
    """Nothing can be authorized against a target that is not connected."""
    with pytest.raises(EnvironmentNotIntegrated):
        SREGymEnvironment(run_id="run-1").bind_executor("any-public-key")


def test_it_is_a_target_environment_with_no_scenarios() -> None:
    env = SREGymEnvironment(run_id="run-1")
    assert isinstance(env, TargetEnvironment) and env.kind == "sregym"
    assert env.list_scenarios() == []


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
async def test_every_operation_raises_not_integrated(operation: str) -> None:
    with pytest.raises(EnvironmentNotIntegrated, match="integrations/sregym/README.md"):
        await OPERATIONS[operation](SREGymEnvironment(run_id="run-1"))


async def test_the_oracle_refuses_instead_of_inventing_health() -> None:
    oracle = EnvironmentHealthOracle(SREGymEnvironment(run_id="run-1"))
    assert oracle.name == "sregym-health-oracle"
    with pytest.raises(EnvironmentNotIntegrated):
        await oracle.probe()


def test_the_sample_config_builds_the_stub() -> None:
    config = EnvironmentConfig.model_validate(yaml.safe_load(SAMPLE_CONFIG.read_text("utf-8")))
    assert config.kind == "sregym"
    env = SREGymEnvironment(run_id="run-1", **config.options)
    assert env.describe().namespace == config.options["namespace"]
