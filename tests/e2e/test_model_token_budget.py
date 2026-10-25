"""The run's model-token budget end to end (review of M3): a stop never hides a request the
model server already answered.

A fake Ollama server answers through the real provider, harness, MCP server and simulator.
"""

from __future__ import annotations

import pytest

from recap.core.config import Settings
from recap.core.models import IncidentState, RunBudget, RunConfig
from recap.providers.factory import create_provider
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from tests.llm_fakes import FakeOllama, OperatorApproves, ScriptedModel

pytestmark = pytest.mark.e2e


async def test_a_refused_corrective_retry_leaves_the_answered_request_in_trace_and_usage(
    settings: Settings,
) -> None:
    """The first answer is prose with no tool call (unusable in tools mode), so RECAP asks
    once more; the server reported so many tokens for the first request that the retry no
    longer fits. The run stops, and the first request is still on the record."""
    limit = 12_000  # room for the first request's reservation (a few thousand tokens)
    answered = limit - 1024  # what the server reports for it: too little room left for more
    fake = FakeOllama(
        [{"content": "It is probably the release.", "prompt_tokens": answered}],
        responder=ScriptedModel(),
    )
    runtime = Runtime(settings)
    runtime.make_provider = lambda config: create_provider(  # type: ignore[method-assign]
        "ollama", runtime.settings, mock_http=fake.mock(), ledger=runtime.ledger
    )
    config = RunConfig(
        provider="ollama",
        use_memory=False,
        strategy="single",
        budget=RunBudget(max_model_tokens=limit),
    )

    record = await runtime.run_incident(
        "bad-deployment", config, approval_channel=OperatorApproves(runtime.approvals)
    )

    assert record.state == IncidentState.BUDGET_EXHAUSTED, record.error
    assert "max_model_tokens" in record.outcome_note
    assert len(fake.chats) == 1  # the retry was never sent
    calls = [e for e in runtime.events.list_events(record.run_id) if e.category == C.LLM_CALL]
    assert [(c.payload["status"], c.payload["input_tokens"]) for c in calls] == [(200, answered)]
    assert record.usage.model_requests == 1
    assert record.usage.actual_input_tokens == answered
