"""The evaluation split guard refuses splits that could leak test scenarios into training."""

from __future__ import annotations

import pytest

from evals.runner import Split, _check_split, load_split
from recap.core.config import Settings
from recap.environments.simulated.engine import SimulatedEnvironment


@pytest.fixture
def scenarios(settings: Settings, tmp_path):  # type: ignore[no-untyped-def]
    env = SimulatedEnvironment(
        run_id="split",
        scenarios_dir=settings.environment.options["scenarios_dir"],
        state_dir=str(tmp_path),
    )
    return {s.id: s for s in env.list_scenarios()}


def test_the_shipped_split_matches_scenario_partitions(scenarios) -> None:  # type: ignore[no-untyped-def]
    _check_split(load_split(), scenarios)  # must not raise


def test_a_test_scenario_in_the_train_split_is_refused(scenarios) -> None:  # type: ignore[no-untyped-def]
    split = Split(name="bad", train=["test-cart-memory-leak"], test=["test-cart-injection"])
    with pytest.raises(ValueError):
        _check_split(split, scenarios)


def test_a_train_scenario_in_the_test_split_is_refused(scenarios) -> None:  # type: ignore[no-untyped-def]
    split = Split(name="bad", train=["train-cart-memory-leak"], test=["train-checkout-bad-release"])
    with pytest.raises(ValueError):
        _check_split(split, scenarios)
