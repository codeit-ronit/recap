"""Tests for the environment registry: configuration in, TargetEnvironment adapter out.

These tests show the adapter is replaceable: the same factory builds the V1 simulator, the
test-only FakeEnvironment (by import path) or any kind registered at runtime.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recap.core.config import EnvironmentConfig, Settings
from recap.core.errors import ConfigError
from recap.environments import registry
from recap.environments.registry import (
    available_kinds,
    create_environment,
    register_environment,
)
from tests.fakes import FakeEnvironment


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a test register kinds without leaking them into other tests."""
    monkeypatch.setattr(registry, "_FACTORIES", dict(registry._FACTORIES))


def test_import_path_kind_builds_that_class_with_the_run_id_and_options(tmp_path: Path):
    config = EnvironmentConfig(
        kind="tests.fakes:FakeEnvironment", options={"state_dir": str(tmp_path)}
    )

    environment = create_environment(config, run_id="run-42")

    assert isinstance(environment, FakeEnvironment)
    # The fake names its state file after the run id, inside the configured state_dir.
    assert environment.path == tmp_path / "fake-run-42.json"


def test_simulated_kind_builds_the_simulator(settings: Settings):
    engine = pytest.importorskip("recap.environments.simulated.engine")

    environment = create_environment(settings.environment, run_id="run-sim")

    assert isinstance(environment, engine.SimulatedEnvironment)
    assert environment.kind == "simulated"


def test_built_in_kinds_are_listed():
    assert {"simulated", "kind", "sregym"} <= set(available_kinds())


def test_kind_builds_the_kubernetes_adapter_without_touching_anything(tmp_path: Path):
    """The Kind adapter (M5) is built by name; building it reads no file and runs nothing."""
    from recap.environments.kubernetes import KubernetesEnvironment

    options = {
        "scenarios_dir": str(tmp_path / "scenarios"),
        "state_dir": str(tmp_path / "sandbox"),
        "client": "kubectl",
        "kubectl": "/opt/homebrew/bin/kubectl",
        "credentials_dir": str(tmp_path / "creds"),
    }

    environment = create_environment(EnvironmentConfig(kind="kind", options=options), "describe")

    assert isinstance(environment, KubernetesEnvironment)
    assert environment.kind == "kind" and environment.describe().is_simulated is False
    assert not (tmp_path / "sandbox").exists() and not (tmp_path / "creds").exists()


def test_unknown_kind_is_a_config_error_that_lists_the_supported_kinds():
    with pytest.raises(ConfigError, match="supported kinds: .*simulated"):
        create_environment(EnvironmentConfig(kind="kubernetes"), run_id="run-1")


@pytest.mark.parametrize("kind", ["pathlib:Path", "tests.fakes:T0"], ids=["class", "instance"])
def test_import_path_must_name_a_target_environment_class(kind: str):
    with pytest.raises(ConfigError, match="not a TargetEnvironment subclass"):
        create_environment(EnvironmentConfig(kind=kind), run_id="run-1")


@pytest.mark.parametrize(
    "kind", ["no_such_package.module:Thing", "tests.fakes:NoSuchClass"], ids=["module", "class"]
)
def test_import_path_that_cannot_be_imported_is_a_config_error(kind: str):
    with pytest.raises(ConfigError, match="cannot import"):
        create_environment(EnvironmentConfig(kind=kind), run_id="run-1")


@pytest.mark.usefixtures("isolated_registry")
def test_registered_kind_is_built_by_its_factory(tmp_path: Path):
    register_environment("fake", FakeEnvironment)

    config = EnvironmentConfig(kind="fake", options={"state_dir": str(tmp_path)})
    environment = create_environment(config, run_id="run-7")

    assert isinstance(environment, FakeEnvironment)
    assert environment.run_id == "run-7"
    assert "fake" in available_kinds()


@pytest.mark.usefixtures("isolated_registry")
def test_factory_that_builds_something_else_is_rejected():
    register_environment("broken", lambda **_: object())

    with pytest.raises(ConfigError, match="did not build a TargetEnvironment"):
        create_environment(EnvironmentConfig(kind="broken"), run_id="run-1")
