"""End to end with an LLM provider: real HTTP protocol, scripted answers, no network.

Each test runs a whole incident: controller, real MCP tool server subprocess, gateway,
simulator, verifier, approval and oracle. The "model" is ScriptedModel behind FakeOllama or
FakeGroq (tests/llm_fakes.py): it answers over the real Ollama/Groq HTTP protocols but does
not reason. So these tests prove the plumbing and the safety properties of an LLM run, not
model quality; real-model runs are measured separately (docs/status.md) and replayed by
test_recorded_model_runs.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from recap.core.config import Settings
from recap.core.ids import utcnow
from recap.core.models import IncidentState, RunConfig, RunRecord, TraceEvent
from recap.providers.factory import REPLAY_ENV, create_provider
from recap.providers.quota import OPT_IN_ENV
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from tests.adversarial.test_audit_oracle_isolation import (
    LABEL_FIELDS,
    ORACLE_SENTINEL,
    SCENARIO_ID_SENTINEL,
    use_sentinel_oracle,
    with_sentinel_scenario,
)
from tests.llm_fakes import (
    FAKE_GROQ_KEY,
    FakeGroq,
    FakeOllama,
    OperatorApproves,
    ScriptedModel,
    error,
)

pytestmark = pytest.mark.e2e  # every test here runs a real incident end to end

LOCAL = RunConfig(provider="ollama", use_memory=False)


def use_fake_model(runtime: Runtime, name: str, fake: FakeOllama | FakeGroq) -> None:
    """Build `name` from configs/providers.yaml for every run, with the network replaced."""

    def make(config: RunConfig) -> Any:
        return create_provider(name, runtime.settings, mock_http=fake.mock(), ledger=runtime.ledger)

    runtime.make_provider = make  # type: ignore[method-assign]


async def run(runtime: Runtime, scenario: str, config: RunConfig) -> RunRecord:
    return await runtime.run_incident(
        scenario, config, approval_channel=OperatorApproves(runtime.approvals)
    )


def events(runtime: Runtime, record: RunRecord, category: str) -> list[TraceEvent]:
    return [e for e in runtime.events.list_events(record.run_id) if e.category == category]


async def test_a_model_driven_run_resolves_through_mcp_with_an_operator_approval(
    settings: Settings,
) -> None:
    runtime = Runtime(settings)
    fake = FakeOllama(responder=ScriptedModel())
    use_fake_model(runtime, "ollama", fake)

    record = await run(runtime, "bad-deployment", LOCAL)

    assert record.state == IncidentState.RESOLVED
    assert record.recovery is not None and record.recovery.verified  # the oracle's own check
    assert record.provider_info is not None and record.provider_info.origin == "local_llm"
    assert record.versions["prompt_version"] == "prompts-v4"
    assert [(a.action, a.target, a.result) for a in record.actions] == [
        ("execute_rollback", "checkout", "recovered")
    ]
    assert record.approvals[0].actor == "test-operator"

    # Every decision came from a model request, and the trace shows each one.
    calls = events(runtime, record, C.LLM_CALL)
    assert len(calls) == len(fake.chats) == record.usage.model_requests
    assert all(e.payload["origin"] == "local_llm" and e.payload["ok"] for e in calls)
    assert record.usage.actual_input_tokens == sum(e.payload["input_tokens"] for e in calls)
    assert record.usage.is_estimate is False

    # The tools that ran are the ones the scripted model asked for, through the gateway.
    ran = [e.payload["tool"] for e in events(runtime, record, C.TOOL_CALL)]
    assert ran[:4] == [
        "get_recent_deployments",
        "get_service_logs",
        "get_service_metrics",
        "propose_rollback",
    ]
    [summary] = events(runtime, record, C.RUN_SUMMARY)
    assert summary.payload["cost"].startswith("$0.00 - local model on this machine")


async def test_no_label_or_oracle_data_reaches_the_model_even_after_a_failed_fix(
    settings: Settings, tmp_path: Path
) -> None:
    """Oracle isolation at the wire: what is SENT to the model, byte for byte.

    The scripted model first proposes a restart (which cannot fix a bad release), so the
    oracle says "not recovered" and the controller investigates again; the sentinel oracle
    adds a made-up service to its private findings. No request body may contain it, the
    scenario's id, or an evaluation label field.
    """
    runtime = Runtime(with_sentinel_scenario(settings, tmp_path))
    probes = use_sentinel_oracle(runtime)
    fake = FakeOllama(responder=ScriptedModel(fixes=["restart_service", "execute_rollback"]))
    use_fake_model(runtime, "ollama", fake)

    record = await run(runtime, SCENARIO_ID_SENTINEL, LOCAL)

    assert [a.result for a in record.actions] == ["not_recovered", "recovered"]
    assert any(ORACLE_SENTINEL in p.unhealthy_services for p in probes)  # the oracle knew
    sent = [json.dumps(body) for body in fake.chats]
    assert len(sent) > 4
    for body in sent:
        assert ORACLE_SENTINEL not in body
        assert SCENARIO_ID_SENTINEL not in body
        for label in LABEL_FIELDS:
            assert f'"{label}"' not in body


async def test_a_recorded_run_replays_offline_to_the_same_outcome(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(settings)
    fake = FakeOllama(responder=ScriptedModel())
    use_fake_model(runtime, "ollama", fake)
    live = await run(runtime, "bad-deployment", LOCAL)
    recording = live.versions["recording"]
    assert recording and (settings.paths.root / recording).is_file()

    replay_runtime = Runtime(settings)  # the real factory: no mock network at all
    monkeypatch.setenv(REPLAY_ENV, str(settings.paths.root / recording))
    replayed = await run(
        replay_runtime, "bad-deployment", RunConfig(provider="replay", use_memory=False)
    )

    assert replayed.state == live.state == IncidentState.RESOLVED
    assert replayed.run_id != live.run_id  # different evidence ID prefixes, remapped
    assert replayed.provider_info is not None and replayed.provider_info.origin == "replay"
    assert replayed.provider_info.replay_of["original_provider"] == "ollama"  # type: ignore[index]
    assert replayed.usage.model_requests == 0  # nothing was sent to any model
    calls = events(replay_runtime, replayed, C.LLM_CALL)
    assert calls and all(e.payload["replayed"] for e in calls)
    tools = lambda r, rt: [e.payload["tool"] for e in events(rt, r, C.TOOL_CALL)]  # noqa: E731
    assert tools(replayed, replay_runtime) == tools(live, runtime)
    [summary] = events(replay_runtime, replayed, C.RUN_SUMMARY)
    assert summary.payload["cost"].startswith("$0.00 - replayed recording")


async def test_a_free_quota_429_defers_the_run_and_the_key_is_never_stored(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OPT_IN_ENV, "1")
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("RECAP_GROQ_FREE_PLAN_CONFIRMED_ON", utcnow().date().isoformat())
    runtime = Runtime(settings)
    fake = FakeGroq([error(429, "rate_limit_exceeded", **{"retry-after": "600"})])
    use_fake_model(runtime, "groq", fake)

    record = await run(runtime, "bad-deployment", RunConfig(provider="groq", use_memory=False))

    assert record.state == IncidentState.QUOTA_EXHAUSTED
    assert record.outcome_note.startswith("deferred:") and "429" in record.outcome_note
    assert record.actions == [] and record.episode_id is None  # nothing decided, nothing learned
    assert len(fake.chats) == 1  # one request, no retry after a long retry-after
    skipped = events(runtime, record, C.MEMORY_SKIP)
    assert "run deferred" in skipped[-1].payload["reason"]
    # The key went on the wire and nowhere else: no file RECAP wrote contains it.
    assert fake.requests[-1].headers["authorization"] == f"Bearer {FAKE_GROQ_KEY}"
    for path in settings.paths.data_dir.rglob("*"):
        if path.is_file():
            assert FAKE_GROQ_KEY.encode() not in path.read_bytes(), path
