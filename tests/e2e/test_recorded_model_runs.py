"""Replay a REAL model run offline: fixtures/recordings/ (recorded with qwen3:8b via Ollama).

The run goes through the whole system (controller, MCP subprocess, gateway, simulator,
verifier, approval, oracle) exactly as live, but every model answer comes from the
recording. It proves, in CI and at $0, that the recorded model's own decisions resolve the
incident through RECAP's real path, and that a replay is always labeled as one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recap.core.config import Settings
from recap.core.models import IncidentState, RunConfig
from recap.providers.factory import REPLAY_ENV
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from tests.llm_fakes import OperatorApproves

pytestmark = pytest.mark.e2e

RECORDINGS = Path(__file__).resolve().parents[2] / "fixtures" / "recordings"
QWEN = RECORDINGS / "ollama-qwen3-8b-bad-deployment.jsonl"


async def test_the_recorded_qwen3_run_resolves_offline_and_says_it_is_a_replay(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REPLAY_ENV, str(QWEN))
    runtime = Runtime(settings)

    record = await runtime.run_incident(
        "bad-deployment",
        RunConfig(provider="replay", use_memory=False),
        approval_channel=OperatorApproves(runtime.approvals),
    )

    assert record.state == IncidentState.RESOLVED
    assert record.recovery is not None and record.recovery.verified
    info = record.provider_info
    assert info is not None and (info.kind, info.origin) == ("replay", "replay")
    assert info.replay_of is not None and info.replay_of["model"] == "qwen3:8b"
    assert record.usage.model_requests == 0  # no model was called in this run
    # The model's own choices, as recorded: its tool calls and its proposal.
    events = runtime.events.list_events(record.run_id)
    tools = [e.payload["tool"] for e in events if e.category == C.TOOL_CALL]
    assert tools[:6] == [
        "get_service_health",
        "get_recent_deployments",
        "get_k8s_like_events",
        "get_service_logs",
        "get_service_metrics",
        "get_service_metrics",
    ]
    [proposal] = record.proposals
    assert (proposal.action, proposal.target, proposal.source) == (
        "execute_rollback",
        "checkout",
        "llm",
    )
    assert proposal.params == {"to_version": "v2.4.0"}
    calls = [e for e in events if e.category == C.LLM_CALL]
    assert len(calls) == 7 and all(e.payload["replayed"] for e in calls)
    assert sum(e.payload["input_tokens"] for e in calls) == 18331  # the original run's counts


def test_recordings_hold_no_secrets_or_local_paths() -> None:
    """Recordings are committed: no key formats, no home folder, no Ollama model files."""
    for path in RECORDINGS.glob("*.jsonl"):
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text and "/home/" not in text, path.name
        for marker in ("gsk_", "sk-ant-", "Bearer ", "authorization", "LICENSE"):
            assert marker not in text, (path.name, marker)
        header = json.loads(text.splitlines()[0])
        assert header["type"] == "header" and header["prompt_version"] == "prompts-v4"
