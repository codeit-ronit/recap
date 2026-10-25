"""A recorded parallel round replays offline whatever the timing of the live run.

Each run forces a parallel round (two specialists at once) against a fake Ollama server,
with one specialist's answers delayed in the live run, then replays the recording with the
replay provider, whose answers arrive at once. Before M3 the replay failed with
ReplayMismatch whenever the live timing differed: specialists read each other's in-flight
evidence, and evidence IDs followed arrival order.

The same holds when timing STOPPED something in the live run: a specialist cut by its
time share, or a shared budget (tool calls, model tokens) that ran out for whichever agent
asked first. The recording keeps where each stop happened, and the replay stops there too.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
import pytest

from recap.core.config import Settings
from recap.core.models import IncidentState, RunBudget, RunConfig, RunRecord
from recap.providers.factory import REPLAY_ENV, create_provider
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from tests.llm_fakes import FakeOllama, OperatorApproves, ScriptedModel

pytestmark = pytest.mark.e2e

PARALLEL = RunConfig(provider="ollama", use_memory=False, strategy="parallel")
TELEMETRY = "telemetry_investigator"


class Delayed:
    """An async handler around FakeOllama that delays some agents' answers.

    `slow_role` names the agent ("*" for every agent); `step` delays only that step's answer.
    """

    def __init__(
        self,
        fake: FakeOllama,
        slow_role: str | None,
        seconds: float = 0.3,
        step: int | None = None,
    ) -> None:
        self.fake, self.slow_role, self.seconds, self.step = fake, slow_role, seconds, step

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chat" and self.slow_role:
            text = json.loads(request.content)["messages"][1]["content"]
            role = re.search(r"Role: (\S+)", text)
            step = re.search(r"This is step (\d+)", text)
            slow = role is not None and self.slow_role in ("*", role.group(1))
            if slow and (self.step is None or (step and int(step.group(1)) == self.step)):
                await asyncio.sleep(self.seconds)  # cancellable, like a slow model
        return self.fake.handle(request)


def tool_calls(runtime: Runtime, record: RunRecord) -> list[tuple[str | None, str, str]]:
    """(agent, tool, evidence ID without the run prefix) for every read, in trace order."""
    events = runtime.events.list_events(record.run_id)
    results = {
        e.payload.get("invocation_id"): e.payload.get("evidence_id")
        for e in events
        if e.category == C.TOOL_RESULT
    }
    return sorted(
        (
            e.agent,
            e.payload["tool"],
            str(results.get(e.payload.get("invocation_id"))).split("-")[-1],
        )
        for e in events
        if e.category == C.TOOL_CALL
    )


async def live_run(
    settings: Settings,
    slow_role: str | None,
    config: RunConfig = PARALLEL,
    **delay: Any,
) -> tuple[Runtime, RunRecord]:
    runtime = Runtime(settings)
    handler = Delayed(FakeOllama(responder=ScriptedModel()), slow_role, **delay)
    mock = httpx.MockTransport(handler)

    def make(config: RunConfig) -> Any:
        return create_provider("ollama", runtime.settings, mock_http=mock, ledger=runtime.ledger)

    runtime.make_provider = make  # type: ignore[method-assign]
    record = await runtime.run_incident(
        "bad-deployment", config, approval_channel=OperatorApproves(runtime.approvals)
    )
    return runtime, record


async def replay_run(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    live: RunRecord,
    config: RunConfig = PARALLEL,
) -> tuple[Runtime, RunRecord]:
    """The live run's recording, replayed offline with the same run config."""
    assert live.provider_info is not None and live.provider_info.recording
    monkeypatch.setenv(REPLAY_ENV, str(settings.paths.root / live.provider_info.recording))
    runtime = Runtime(settings)
    record = await runtime.run_incident(
        "bad-deployment",
        config.model_copy(update={"provider": "replay"}),
        approval_channel=OperatorApproves(runtime.approvals),
    )
    return runtime, record


def ending(runtime: Runtime, record: RunRecord) -> dict[str, Any]:
    """How a run ended, in terms a replay must reproduce exactly: its state and note, every
    agent's finding, the actions, every tool call and what the budget counted."""
    [summary] = [
        e for e in runtime.events.list_events(record.run_id) if e.category == C.RUN_SUMMARY
    ]
    budget = {k: v for k, v in summary.payload["budget"].items() if k != "elapsed_seconds"}
    return {
        "state": record.state,
        "note": record.outcome_note,
        "error": record.error,
        "findings": [
            (f.agent_role, f.round, f.outcome, f.error, [h.claim for h in f.hypotheses])
            for f in record.findings
        ],
        "actions": [(a.action, a.target, a.params, a.result) for a in record.actions],
        "tools": tool_calls(runtime, record),
        "budget": budget,
    }


@pytest.mark.parametrize(
    "slow_role", [None, "telemetry_investigator", "infrastructure_investigator"]
)
async def test_a_parallel_round_replays_whatever_the_live_timing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, slow_role: str | None
) -> None:
    live_runtime, live = await live_run(settings, slow_role)
    assert live.state == IncidentState.RESOLVED
    assert any(s.mode.value == "parallel" for s in live.strategies)
    assert live.provider_info is not None and live.provider_info.recording

    monkeypatch.setenv(REPLAY_ENV, str(settings.paths.root / live.provider_info.recording))
    replay_runtime = Runtime(settings)
    replay = await replay_runtime.run_incident(
        "bad-deployment",
        PARALLEL.model_copy(update={"provider": "replay"}),
        approval_channel=OperatorApproves(replay_runtime.approvals),
    )
    assert replay.state == IncidentState.RESOLVED, replay.error  # no ReplayMismatch
    assert tool_calls(replay_runtime, replay) == tool_calls(live_runtime, live)


async def test_parallel_evidence_ids_do_not_depend_on_which_agent_was_slower(
    settings: Settings,
) -> None:
    runtime_a, run_a = await live_run(settings, "telemetry_investigator")
    runtime_b, run_b = await live_run(settings, "infrastructure_investigator")
    assert tool_calls(runtime_a, run_a) == tool_calls(runtime_b, run_b)


# --- stops that depended on timing (review of M3) -------------------------------------------


async def test_a_specialist_cut_by_its_time_share_is_cut_the_same_way_in_the_replay(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telemetry's second answer comes after its share of the run's time (0.7 of the time
    left): the live run cuts it and resolves with infrastructure's finding. A replay answers
    at once, so only the recording can say where telemetry was cut. (The run's 5 s leave
    the rest of the run more than a second after the cut, also on a slow machine.)"""
    timed = PARALLEL.model_copy(update={"budget": RunBudget(max_elapsed_seconds=5)})
    live_runtime, live = await live_run(settings, TELEMETRY, timed, seconds=10, step=2)
    assert live.state == IncidentState.RESOLVED, live.error
    telemetry = next(f for f in live.findings if f.agent_role.value == TELEMETRY)
    assert telemetry.outcome == "error" and (telemetry.error or "").startswith("timeout after")

    replay_runtime, replay = await replay_run(settings, monkeypatch, live, timed)
    assert ending(replay_runtime, replay) == ending(live_runtime, live), replay.error


@pytest.mark.parametrize(
    ("budget", "slow_role", "seconds"),
    [
        # The agents' requests overlap: one reserves while the other's is still out.
        (RunBudget(max_model_tokens=6000), "*", 0.05),
        # Infrastructure's calls take the last of the shared tool calls first.
        (RunBudget(max_tool_calls=3), TELEMETRY, 0.3),
    ],
    ids=["model_tokens", "tool_calls"],
)
async def test_a_parallel_run_stopped_by_a_shared_budget_stops_the_same_way_in_the_replay(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    budget: RunBudget,
    slow_role: str,
    seconds: float,
) -> None:
    """Which agent reaches a shared limit first is timing; the replay must refuse the same
    agent at the same point, not whoever asks first when answers come at once."""
    tight = PARALLEL.model_copy(update={"budget": budget})
    live_runtime, live = await live_run(settings, slow_role, tight, seconds=seconds)
    assert live.state == IncidentState.BUDGET_EXHAUSTED, live.error

    replay_runtime, replay = await replay_run(settings, monkeypatch, live, tight)
    assert ending(replay_runtime, replay) == ending(live_runtime, live), replay.error
