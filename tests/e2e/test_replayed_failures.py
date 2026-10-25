"""A request that failed in the live run fails the same way in its replay (review of M3).

The local model server could not be reached for one request (here the embedding of an
agent's runbook search), and the live run went on with a failed tool call. The recording
keeps the failure, so the replay fails that request the same way and goes on as the live
run did, instead of stopping with ReplayMismatch for a request it has no answer to.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from recap.core.config import Settings
from recap.core.models import IncidentState, RunConfig, RunRecord
from recap.providers.factory import create_embedder, create_provider
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from tests.e2e.test_parallel_replay import ending, replay_run
from tests.e2e.test_runbook_search import QUERY, Searcher
from tests.llm_fakes import FakeOllama, OperatorApproves

pytestmark = pytest.mark.e2e

HYBRID = RunConfig(provider="ollama", use_memory=False, retrieval="hybrid")


class Unreachable:
    """FakeOllama, except that embedding an agent's search query cannot reach the server."""

    def __init__(self, fake: FakeOllama) -> None:
        self.fake = fake

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/embed":
            inputs = json.loads(request.content).get("input") or []
            if any(QUERY in text for text in inputs):
                raise httpx.ConnectError("connection refused", request=request)
        return self.fake.handle(request)


def failed_tool_calls(runtime: Runtime, record: RunRecord) -> list[tuple[str | None, str]]:
    return [
        (e.agent, e.payload["error"])
        for e in runtime.events.list_events(record.run_id)
        if e.category == C.TOOL_RESULT and not e.payload["ok"]
    ]


async def test_a_search_whose_embedding_failed_fails_the_same_way_in_the_replay(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(settings)
    mock = httpx.MockTransport(Unreachable(FakeOllama(responder=Searcher())))

    def make(config: RunConfig) -> Any:
        return create_provider("ollama", runtime.settings, mock_http=mock, ledger=runtime.ledger)

    runtime.make_provider = make  # type: ignore[method-assign]
    runtime.make_embedder = (  # type: ignore[method-assign]
        lambda provider: create_embedder(runtime.settings, provider, mock_http=mock)
    )
    live = await runtime.run_incident(
        "bad-deployment", HYBRID, approval_channel=OperatorApproves(runtime.approvals)
    )
    assert live.state == IncidentState.RESOLVED, live.error
    [(agent, error)] = failed_tool_calls(runtime, live)
    assert agent == "investigator" and "could not reach" in error

    replay_runtime, replay = await replay_run(settings, monkeypatch, live, HYBRID)
    assert failed_tool_calls(replay_runtime, replay) == [(agent, error)]
    assert ending(replay_runtime, replay) == ending(runtime, live), replay.error
