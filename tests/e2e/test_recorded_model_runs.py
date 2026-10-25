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
from recap.providers.protocol import PROMPT_VERSION
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from tests.llm_fakes import OperatorApproves

pytestmark = pytest.mark.e2e

RECORDINGS = Path(__file__).resolve().parents[2] / "fixtures" / "recordings"
QWEN = RECORDINGS / "ollama-qwen3-8b-bad-deployment.jsonl"
# The M3 deliverable: two specialists in parallel on the held-out unfamiliar incident, the
# fix approved by the owner at the terminal (evals/live/2026-10-25-...-v4.7/).
QWEN_PARALLEL = RECORDINGS / "ollama-qwen3-8b-unfamiliar-parallel.jsonl"


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
    assert tools[:5] == [
        "get_service_health",
        "get_recent_deployments",
        "get_service_logs",
        "get_k8s_like_events",
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
    assert sum(e.payload["input_tokens"] for e in calls) == 19946  # the original run's counts
    # Which server and weights answered the original run, from its recorded metadata.
    assert info.server is not None and info.server["ollama_version"] == "0.34.4"
    assert info.server["weights"] == (
        "sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f"
    )
    # The replay runs with the RECORDED run's limits (its provider's budget), not the
    # replay entry's defaults, so every prompt says the same "step N of M".
    assert record.incident is not None
    assert record.incident.budget.max_elapsed_seconds == 1800


async def test_the_recorded_two_agent_run_resolves_the_unfamiliar_incident_offline(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real model's two-specialist investigation replays through RECAP's whole path.

    The answers are the recorded qwen3:8b's; the rest runs now: two specialists at once,
    each with its own tools and view, the verifier between rounds, the router, the safety
    policy, the approval and the oracle. The approval channel does not change what the
    model sees, so the replay's approver stands in for the owner's terminal.
    """
    monkeypatch.setenv(REPLAY_ENV, str(QWEN_PARALLEL))
    runtime = Runtime(settings)
    record = await runtime.run_incident(
        "m3-inventory-leak-behind-frontend",
        RunConfig(provider="replay", strategy="parallel"),
        approval_channel=OperatorApproves(runtime.approvals),
    )

    assert record.state == IncidentState.RESOLVED, record.error
    assert record.recovery is not None and record.recovery.verified
    info = record.provider_info
    assert info is not None and (info.kind, info.origin) == ("replay", "replay")
    assert info.replay_of is not None and info.replay_of["model"] == "qwen3:8b"
    assert record.usage.model_requests == 0  # no model was called in this run
    events = runtime.events.list_events(record.run_id)
    # Two rounds of two specialists; round 2 got the verifier's leads for the chain.
    routing = [(e.payload["round"], e.payload["mode"]) for e in events if e.category == C.ROUTING]
    assert routing == [(1, "parallel"), (2, "parallel")]
    rounds = [e.payload for e in events if e.category == C.INVESTIGATION_ROUND]
    assert rounds[0]["leads"] == [] and any("inventory" in lead for lead in rounds[1]["leads"])
    assert {(f.agent_role.value, f.round) for f in record.findings} == {
        ("telemetry_investigator", 1),
        ("infrastructure_investigator", 1),
        ("telemetry_investigator", 2),
        ("infrastructure_investigator", 2),
    }
    callers = {e.payload["caller"] for e in events if e.category == C.TOOL_CALL}
    assert callers == {"telemetry_investigator", "infrastructure_investigator"}
    # The verifier supported the deepest cause; the model proposed the fix for it.
    assert record.verification is not None and record.verification.best is not None
    best = record.verification.best
    assert (best.mechanism.value, best.target_service) == ("resource_pressure", "inventory")
    [proposal] = record.proposals
    assert (proposal.action, proposal.target, proposal.source) == (
        "restart_service",
        "inventory",
        "llm",
    )
    assert [(a.action, a.target, a.result) for a in record.actions] == [
        ("restart_service", "inventory", "recovered")
    ]
    calls = [e for e in events if e.category == C.LLM_CALL]
    assert len(calls) == 14 and all(e.payload["replayed"] for e in calls)
    assert sum(e.payload["input_tokens"] for e in calls) == 52585  # the original run's counts


def test_recordings_hold_no_secrets_or_local_paths() -> None:
    """Recordings are committed: no key formats, no home folder, no Ollama model files."""
    for path in RECORDINGS.glob("*.jsonl"):
        text = path.read_text(encoding="utf-8")
        # Booleans on purpose: on failure pytest must not print the file into the CI log.
        for marker in ("/Users/", "/home/", "gsk_", "sk-ant-", "Bearer ", "authorization"):
            found = marker in text
            assert not found, (path.name, marker)
        found = "LICENSE" in text
        assert not found, (path.name, "LICENSE")
        header = json.loads(text.splitlines()[0])
        assert header["type"] == "header" and header["prompt_version"] == PROMPT_VERSION
