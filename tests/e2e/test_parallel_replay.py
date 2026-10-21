"""A recorded parallel round replays offline whatever the timing of the live run.

Each run forces a parallel round (two specialists at once) against a fake Ollama server,
with one specialist's answers delayed in the live run, then replays the recording with the
replay provider, whose answers arrive at once. Before M3 the replay failed with
ReplayMismatch whenever the live timing differed: specialists read each other's in-flight
evidence, and evidence IDs followed arrival order.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
import pytest

from recap.core.config import Settings
from recap.core.models import IncidentState, RunConfig, RunRecord
from recap.providers.factory import REPLAY_ENV, create_provider
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from tests.llm_fakes import FakeOllama, OperatorApproves, ScriptedModel

pytestmark = pytest.mark.e2e

PARALLEL = RunConfig(provider="ollama", use_memory=False, strategy="parallel")


class Delayed:
    """An async handler around FakeOllama that delays some agents' answers."""

    def __init__(self, fake: FakeOllama, slow_role: str | None, seconds: float = 0.3) -> None:
        self.fake, self.slow_role, self.seconds = fake, slow_role, seconds

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chat" and self.slow_role:
            text = json.loads(request.content)["messages"][1]["content"]
            role = re.search(r"Role: (\S+)", text)
            if role and role.group(1) == self.slow_role:
                await asyncio.sleep(self.seconds)
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


async def live_run(settings: Settings, slow_role: str | None) -> tuple[Runtime, RunRecord]:
    runtime = Runtime(settings)
    mock = httpx.MockTransport(Delayed(FakeOllama(responder=ScriptedModel()), slow_role))

    def make(config: RunConfig) -> Any:
        return create_provider("ollama", runtime.settings, mock_http=mock, ledger=runtime.ledger)

    runtime.make_provider = make  # type: ignore[method-assign]
    record = await runtime.run_incident(
        "bad-deployment", PARALLEL, approval_channel=OperatorApproves(runtime.approvals)
    )
    return runtime, record


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
