"""On a target that is not simulated, a hosted model may not see the target's data.

The controller refuses a free hosted provider (unless the owner explicitly permits it for this
process) and a paid provider (always), after the provider is built and before the environment
is reset: nothing is observed, and no run record or approval request exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recap.core.config import Settings
from recap.core.errors import ConfigError
from recap.core.models import IncidentState, RunConfig
from recap.harness.controller import HOSTED_ON_LIVE_ENV
from recap.providers.deterministic import DeterministicProvider
from recap.safety.approvals import ConsoleApprovalChannel
from tests.e2e.test_golden_replay import not_simulated
from tests.llm_fakes import OperatorApproves


def posing_as(origin: str) -> DeterministicProvider:
    """The scripted policy labelled as another kind of provider: the label is what is checked."""
    provider = DeterministicProvider()
    provider.info = provider.info.model_copy(update={"origin": origin})
    return provider


@pytest.mark.parametrize("origin", ["free_hosted_llm", "paid_llm"])
async def test_a_hosted_model_is_refused_before_a_live_target_is_touched(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    monkeypatch.delenv(HOSTED_ON_LIVE_ENV, raising=False)
    state = tmp_path / "state"  # where the fake keeps its world, from its reset on
    runtime = not_simulated(settings, state)
    runtime.make_provider = lambda config: posing_as(origin)  # type: ignore[method-assign]
    with pytest.raises(ConfigError, match="may not see a live target's data"):
        await runtime.run_incident(
            "fake-bad-release", RunConfig(), approval_channel=OperatorApproves(runtime.approvals)
        )
    assert not state.exists()  # never reset: nothing was observed
    assert runtime.runs.list(10) == [] and runtime.approvals.pending() == []


async def test_the_owner_may_permit_a_free_hosted_plan_but_never_a_paid_one(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HOSTED_ON_LIVE_ENV, "1")
    runtime = not_simulated(settings, tmp_path / "state")
    runtime.make_provider = lambda config: posing_as("paid_llm")  # type: ignore[method-assign]
    with pytest.raises(ConfigError, match="paid_llm provider may not see"):
        await runtime.run_incident("fake-bad-release", RunConfig())

    runtime.make_provider = lambda config: posing_as("free_hosted_llm")  # type: ignore[method-assign]
    record = await runtime.run_incident(
        "fake-bad-release",
        RunConfig(memory_write=False),
        approval_channel=OperatorApproves(runtime.approvals),
    )
    assert record.state == IncidentState.RESOLVED, record.error


async def test_a_local_model_or_the_scripted_policy_still_runs_there(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(HOSTED_ON_LIVE_ENV, raising=False)
    runtime = not_simulated(settings, tmp_path / "state")
    runtime.make_provider = lambda config: posing_as("local_llm")  # type: ignore[method-assign]
    record = await runtime.run_incident(
        "fake-bad-release",
        RunConfig(memory_write=False),
        approval_channel=OperatorApproves(runtime.approvals),
    )
    assert record.state == IncidentState.RESOLVED, record.error


async def test_a_console_that_does_not_know_the_target_is_live_is_refused(
    settings: Settings, tmp_path: Path
) -> None:
    """Fail closed: such a console would accept a plain "y" for a live write."""
    state = tmp_path / "state"
    runtime = not_simulated(settings, state)
    blind = ConsoleApprovalChannel(runtime.approvals)  # no environment given
    with pytest.raises(ConfigError, match="live target's description"):
        await runtime.run_incident("fake-bad-release", RunConfig(), approval_channel=blind)
    assert not state.exists() and runtime.runs.list(10) == []
    informed = ConsoleApprovalChannel(runtime.approvals, environment=runtime.environment_info())
    assert informed.live
