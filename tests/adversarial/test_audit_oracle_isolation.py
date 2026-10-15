"""AUDIT-2.3: oracle and label leakage. A provider (the scripted policy today, an LLM later)
must never be given what only the private recovery oracle knows, or the scenario's
evaluation labels.

The finding, in simple words:

* Oracle leak. The recovery oracle reads the environment's noise-free ground truth (it is
  not an MCP tool) so it can judge, independently of the agents, whether a fix worked.
  When a fix fails, the controller starts a new investigation with
  `focus = after.unhealthy_services` (recap/harness/controller.py), i.e. the oracle's
  private list. That list travels through AgentRunPlan.focus_services into every
  InvestigationRequest, and the prompt renderer shows it to an LLM as "RECAP's verifier
  asked to look closer at: ...". In the shipped bad-deployment scenario the list names
  `frontend`, a service no agent had looked at, and the retry agents start probing it.
* Label leak. Every InvestigationRequest and ProposalRequest carries the whole Incident,
  including `scenario_id`, and the recalled memory hits carry each past episode's
  `scenario_id` and `failure_family` (the scenario's ground-truth label). The prompt
  renderers skip those fields today, but a provider receives the full objects, so any
  provider that reads or dumps them gets the answer key.

How the tests reproduce it, with the real pipeline (simulator, MCP subprocess,
controller, gateway) and the scripted policy wrapped so it records every request:

1. A wrapped oracle also reports a made-up service, ORACLE_SENTINEL, as unhealthy. No
   service, tool or document has that name, so if it ever shows up in a provider request
   or prompt, oracle data leaked there. The first fix fails, which forces a retry.
2. A second run recalls the episode a first run stored. No provider request may contain
   a `scenario_id` or `failure_family` field, or the scenario's id anywhere.
3. Guard (passes today): the rendered prompts never show the scenario id or those labels.

Tests 2 and 3 run a copy of bad-deployment saved under a made-up id, SCENARIO_ID_SENTINEL.
Runbook chunk ids such as "runbook-bad-deployment#remediation-1" legitimately contain the
text "bad-deployment", but the made-up id appears nowhere else, so a plain text search is
exact.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from recap.core.config import Settings
from recap.core.interfaces import TargetEnvironment
from recap.core.models import (
    InvestigationDecision,
    InvestigationRequest,
    OracleProbe,
    ProposalDecision,
    ProposalRequest,
    RunConfig,
)
from recap.harness.controller import ControllerDeps
from recap.providers.deterministic import DeterministicProvider
from recap.providers.prompts import render_investigation_prompt, render_proposal_prompt
from recap.runtime import Runtime
from recap.verification.oracle import EnvironmentHealthOracle

ORACLE_SENTINEL = "oracle-only-sentinel"  # a "service" that exists only in oracle output
SCENARIO_ID_SENTINEL = "scenario-id-sentinel"  # a scenario id that appears nowhere else
LABEL_FIELDS = ("scenario_id", "failure_family")  # evaluation bookkeeping, never for agents
# Without runbooks or memory the scripted policy can only restart checkout, which cannot
# fix a bad release: the oracle says "not recovered" and the controller investigates again.
RESTART_ONLY = RunConfig(approval_mode="auto", use_rag=False, use_memory=False, memory_write=False)

ProviderRequest = InvestigationRequest | ProposalRequest


# --- helpers --------------------------------------------------------------------------------


class RecordingProvider(DeterministicProvider):
    """The scripted policy, but it keeps every request it gets: exactly what an LLM would see."""

    def __init__(self, familiar_similarity: float) -> None:
        super().__init__(familiar_similarity)
        self.requests: list[ProviderRequest] = []

    async def next_action(self, request: InvestigationRequest) -> InvestigationDecision:
        self.requests.append(request)
        return await super().next_action(request)

    async def propose_mitigation(self, request: ProposalRequest) -> ProposalDecision:
        self.requests.append(request)
        return await super().propose_mitigation(request)


def record_requests(runtime: Runtime) -> RecordingProvider:
    """Make the runtime's next runs use a RecordingProvider, and return it."""
    # Same "familiar memory" threshold as the provider factory, so behaviour is unchanged.
    recorder = RecordingProvider(runtime.settings.budget.routing.familiar_similarity)
    runtime.make_provider = lambda config: recorder  # type: ignore[method-assign]
    return recorder


class SentinelOracle(EnvironmentHealthOracle):
    """The real health oracle, except a broken system also reports ORACLE_SENTINEL."""

    def __init__(
        self, environment: TargetEnvironment, max_error_rate: float, probes: list[OracleProbe]
    ) -> None:
        super().__init__(environment, max_error_rate)
        self.probes = probes  # every probe this oracle returned, so the test can inspect them

    async def probe(self) -> OracleProbe:
        """Probe for real; when something is unhealthy, add the sentinel to the findings."""
        result = await super().probe()
        if not result.healthy:  # only a broken system gets it, so the probe stays consistent
            result = result.model_copy(
                update={
                    "unhealthy_services": [*result.unhealthy_services, ORACLE_SENTINEL],
                    "signals": {
                        **result.signals,
                        ORACLE_SENTINEL: {"service": ORACLE_SENTINEL, "error_rate": 1.0},
                    },
                }
            )
        self.probes.append(result)
        return result


def use_sentinel_oracle(runtime: Runtime) -> list[OracleProbe]:
    """Plug SentinelOracle into every controller the runtime builds; return its probe log."""
    probes: list[OracleProbe] = []
    original_deps = runtime.controller_deps

    def deps_with_sentinel_oracle() -> ControllerDeps:
        # ControllerDeps.oracle_factory is the documented hook for bringing another oracle.
        return dataclasses.replace(
            original_deps(),
            oracle_factory=lambda environment, max_error_rate: SentinelOracle(
                environment, max_error_rate, probes
            ),
        )

    runtime.controller_deps = deps_with_sentinel_oracle  # type: ignore[method-assign]
    return probes


def with_scenarios(settings: Settings, scenarios_dir: Path) -> Settings:
    """Settings that read scenarios from `scenarios_dir` (as in test_controller_paths.py)."""
    options = dict(settings.environment.options, scenarios_dir=str(scenarios_dir))
    environment = settings.environment.model_copy(update={"options": options})
    return settings.model_copy(update={"environment": environment})


def with_sentinel_scenario(settings: Settings, tmp_path: Path) -> Settings:
    """Settings whose only scenario is bad-deployment copied under SCENARIO_ID_SENTINEL."""
    shipped = Path(settings.environment.options["scenarios_dir"]) / "bad-deployment.yaml"
    # Only the id changes, so the copy behaves exactly like the shipped scenario.
    text = shipped.read_text(encoding="utf-8").replace(
        "id: bad-deployment", f"id: {SCENARIO_ID_SENTINEL}"
    )
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    (scenarios / f"{SCENARIO_ID_SENTINEL}.yaml").write_text(text, encoding="utf-8")
    return with_scenarios(settings, scenarios)


async def record_run_that_recalls_memory(runtime: Runtime) -> tuple[str, list[ProviderRequest]]:
    """Run the sentinel scenario twice: the first run stores an episode, the second recalls it.

    Returns the stored episode's id and every request the provider got in the second run.
    """
    first = await runtime.run_incident(SCENARIO_ID_SENTINEL, RunConfig(approval_mode="auto"))
    assert first.episode_id is not None, "the first run should store an episode in memory"
    recorder = record_requests(runtime)
    second = RunConfig(approval_mode="auto", memory_write=False)  # memory on, read-only
    await runtime.run_incident(SCENARIO_ID_SENTINEL, second)
    return first.episode_id, recorder.requests


def rendered_prompt(request: ProviderRequest) -> str:
    """The user message an LLM provider would send for this request."""
    if isinstance(request, InvestigationRequest):
        return render_investigation_prompt(request)
    return render_proposal_prompt(request)


def label_fields(node: Any, path: str = "") -> list[str]:
    """Paths of every scenario_id / failure_family key in a dumped model, at any depth."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if key in LABEL_FIELDS:
                found.append(child)
            found += label_fields(value, child)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found += label_fields(item, f"{path}[{index}]")
    return found


def around(text: str, needle: str, width: int = 70) -> str:
    """A short piece of `text` around the first `needle`, so a failure shows where it was."""
    at = text.find(needle)
    return "..." + text[max(0, at - width) : at + len(needle) + width] + "..."


# --- tests ----------------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-2.3: oracle unhealthy_services become the retry's focus_services; fixed in M1",
)
async def test_oracle_findings_never_reach_the_provider_after_a_failed_fix(
    settings: Settings,
) -> None:
    """After a failed fix, the retry investigation must not see what only the oracle knows."""
    runtime = Runtime(settings)
    probes = use_sentinel_oracle(runtime)
    recorder = record_requests(runtime)

    record = await runtime.run_incident("bad-deployment", RESTART_ONLY)

    # The run really took the retry path, judged by the sentinel oracle.
    assert record.actions and record.actions[0].result == "not_recovered"
    assert any(ORACLE_SENTINEL in probe.unhealthy_services for probe in probes)
    first_proposal = next(
        i for i, request in enumerate(recorder.requests) if isinstance(request, ProposalRequest)
    )
    retry_requests = recorder.requests[first_proposal + 1 :]
    assert any(isinstance(request, InvestigationRequest) for request in retry_requests)

    # The oracle's private findings must be in no request object and no rendered prompt.
    for number, request in enumerate(recorder.requests, 1):
        where = f"request {number} ({type(request).__name__})"
        prompt = rendered_prompt(request)
        assert ORACLE_SENTINEL not in prompt, f"{where} prompt: {around(prompt, ORACLE_SENTINEL)}"
        as_json = request.model_dump_json()
        assert ORACLE_SENTINEL not in as_json, f"{where} JSON: {around(as_json, ORACLE_SENTINEL)}"


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-2.3: provider requests carry scenario_id and failure_family; fixed in M1",
)
async def test_provider_requests_carry_no_scenario_labels(
    settings: Settings, tmp_path: Path
) -> None:
    """No request a provider gets may hold the scenario id or a label field, even via memory."""
    runtime = Runtime(with_sentinel_scenario(settings, tmp_path))
    episode_id, requests = await record_run_that_recalls_memory(runtime)

    # The remembered episode really reached the provider (otherwise this test proves nothing).
    assert any(episode_id in request.model_dump_json() for request in requests)

    for number, request in enumerate(requests, 1):
        where = f"request {number} ({type(request).__name__})"
        fields = label_fields(request.model_dump(mode="json"))
        assert fields == [], f"{where} has label fields: {fields}"
        as_json = request.model_dump_json()
        assert SCENARIO_ID_SENTINEL not in as_json, (
            f"{where} JSON: {around(as_json, SCENARIO_ID_SENTINEL)}"
        )


async def test_rendered_prompts_never_show_scenario_labels(
    settings: Settings, tmp_path: Path
) -> None:
    """Guard: the prompt text an LLM would read already leaves out the scenario's labels."""
    runtime = Runtime(with_sentinel_scenario(settings, tmp_path))
    episode_id, requests = await record_run_that_recalls_memory(runtime)
    prompts = [rendered_prompt(request) for request in requests]

    # The remembered episode really is in the prompts (as a past incident).
    assert any(episode_id in prompt for prompt in prompts)

    for number, prompt in enumerate(prompts, 1):
        for label in (SCENARIO_ID_SENTINEL, *LABEL_FIELDS):
            assert label not in prompt, f"prompt {number}: {around(prompt, label)}"
