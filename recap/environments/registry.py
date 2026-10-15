"""Environment registry: the single place that maps configuration to infrastructure.

`configs/environment.yaml` names an environment `kind`, and `create_environment` builds the
matching TargetEnvironment adapter. No other code constructs an environment: agents, RAG,
memory, routing, guardrails and the harness only ever see the TargetEnvironment interface
(through MCP tools and the recovery oracle). So swapping the simulator for SREGym/Kubernetes
is a config change plus a new adapter class; the rest of the code stays the same. (Data such
as log conventions, allowlists and runbooks must still match the new target.)

    kind                       adapter
    -------------------------  ---------------------------------------------------------
    simulated                  SimulatedEnvironment - deterministic local sandbox (V1 default)
    sregym                     SREGymEnvironment - interface only, NOT integrated in V1
    package.module:ClassName   any importable TargetEnvironment subclass
                               (the tests use "tests.fakes:FakeEnvironment")

The controller process (for the oracle) and the MCP server subprocess (for the tools) both call
`create_environment` with the same config and run id, so they look at the same sandbox state.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from recap.core.config import EnvironmentConfig
from recap.core.errors import ConfigError
from recap.core.interfaces import TargetEnvironment

# Anything that can be called as factory(run_id=..., **options) and returns an environment.
# A TargetEnvironment subclass itself qualifies.
EnvironmentFactory = Callable[..., TargetEnvironment]


def _simulated(**options: Any) -> TargetEnvironment:
    """Build the V1 simulator."""
    from recap.environments.simulated.engine import SimulatedEnvironment

    return SimulatedEnvironment(**options)


def _sregym(**options: Any) -> TargetEnvironment:
    """Build the SREGym adapter (an interface only in V1; it reports that it is not integrated)."""
    from integrations.sregym.adapter import SREGymEnvironment

    return SREGymEnvironment(**options)


# Built-in kinds. The imports live inside the factories, so only the chosen adapter is loaded.
_FACTORIES: dict[str, EnvironmentFactory] = {"simulated": _simulated, "sregym": _sregym}


def register_environment(kind: str, factory: EnvironmentFactory) -> None:
    """Add (or replace) an environment kind; `factory(run_id=..., **options)` must build it."""
    _FACTORIES[kind] = factory


def available_kinds() -> list[str]:
    """Registered kinds. An import path such as 'package.module:ClassName' works as well."""
    return sorted(_FACTORIES)


def create_environment(config: EnvironmentConfig, run_id: str) -> TargetEnvironment:
    """Build the TargetEnvironment adapter named by `config.kind` for one run."""
    factory = _FACTORIES.get(config.kind)
    if factory is None:
        factory = _import_environment_class(config.kind)
    environment = factory(run_id=run_id, **config.options)
    # A registered factory could return anything; fail here, not deep inside a run.
    if not isinstance(environment, TargetEnvironment):
        raise ConfigError(f"environment kind {config.kind!r} did not build a TargetEnvironment")
    return environment


def _import_environment_class(kind: str) -> type[TargetEnvironment]:
    """Resolve 'package.module:ClassName' to a TargetEnvironment subclass or raise ConfigError."""
    module_name, separator, class_name = kind.partition(":")
    if not (separator and module_name and class_name):
        raise ConfigError(
            f"unknown environment kind {kind!r}; supported kinds: "
            f"{', '.join(available_kinds())}, or an import path 'package.module:ClassName'"
        )
    try:
        candidate = getattr(importlib.import_module(module_name), class_name)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(f"cannot import environment class {kind!r}: {exc}") from exc
    # Check the type before calling it, so a typo can never construct some unrelated class.
    if not (isinstance(candidate, type) and issubclass(candidate, TargetEnvironment)):
        raise ConfigError(f"{kind!r} is not a TargetEnvironment subclass")
    return candidate
