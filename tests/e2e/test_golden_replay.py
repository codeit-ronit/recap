"""The golden demo in CI: a REAL two-agent model run, replayed offline through RECAP's whole path.

The recording is the M3 deliverable (manifest id `qwen3-8b-unfamiliar-parallel`): qwen3:8b on
the held-out m3-inventory-leak-behind-frontend incident, two specialists in parallel for two
rounds, the fix approved by the owner at a terminal. A replay answers every model request
from that recording (no network, no model) while everything else runs now: the controller,
the MCP tool server subprocess, the gateway and its policy, the simulator, the verifier, a
fresh approval, the signed write and the oracle. These tests check what a reviewer of the
demo relies on:

  * the replay is chosen by its manifest id, its bytes are checked, and it says what it is
    (origin replay, the fixture, the recording, a simulated target);
  * every event has the payload its readers use (dashboard, API, exports);
  * the fix ran only through the exact approval chain, and nothing was written before it;
  * the recovery verdict is the oracle's reading of the simulated target;
  * the same ending whichever specialist's tool calls are slow, with independent views;
  * a fixture with one changed byte is refused before any request is answered;
  * a replay is refused outright against an environment that is not simulated.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from recap.core.config import EnvironmentConfig, Settings, load_settings
from recap.core.errors import ConfigError
from recap.core.ids import canonical_json
from recap.core.models import (
    SCHEMA_VERSION,
    DiscoveredTool,
    EnvironmentInfo,
    IncidentState,
    RawToolResult,
    RunConfig,
    RunRecord,
    ToolRisk,
    TraceEvent,
)
from recap.environments.registry import create_environment
from recap.harness.budget import ACTOR
from recap.providers import factory, recordings
from recap.providers.factory import REPLAY_ENV, create_provider
from recap.providers.transport import ReplayTransport, normalize
from recap.runtime import Runtime, stdio_transport
from recap.telemetry.tracer import TraceCategory as C
from recap.tools.mcp_client import McpToolClient
from recap.verification.oracle import EnvironmentHealthOracle
from tests.fakes import FakeEnvironment
from tests.llm_fakes import OperatorApproves

pytestmark = pytest.mark.e2e

GOLDEN = "qwen3-8b-unfamiliar-parallel"
SCENARIO = "m3-inventory-leak-behind-frontend"
TELEMETRY = "telemetry_investigator"
INFRASTRUCTURE = "infrastructure_investigator"
SPECIALISTS = (TELEMETRY, INFRASTRUCTURE)
# Inside a parallel round each specialist's evidence IDs carry its own letter, "ev-<run>-r2i01"
# for round 2, infrastructure, first item (recap/context/evidence.py).
ROLE_CODES = {TELEMETRY: "t", INFRASTRUCTURE: "i"}
ROUND_EVIDENCE = re.compile(r"^ev-[0-9a-f]{6}-r(\d+)([a-z])\d{2}$")
LOCAL_PATHS = ("/Users/", "/home/", "/private/", "/var/folders/")


def keys(text: str) -> frozenset[str]:
    return frozenset(text.split())


# The payload keys each event category must carry: what the dashboard
# (apps/dashboard/src/types.ts), the API and the run export (scripts/export_run_trace.py) read.
# An event may carry more. verification.evidence has two shapes: a report, a proposal check.
REQUIRED_KEYS: dict[str, tuple[frozenset[str], ...]] = {
    C.STATE: (keys("from to reason"),),
    C.TOOL_DISCOVERY: (keys("transport advertised usable unknown_tools missing_tools"),),
    C.RETRIEVAL: (keys("stage reason query mode retriever results"),),
    C.MEMORY_SEARCH: (keys("readable_partitions created_before semantic hits"),),
    C.ROUTING: (keys("round mode rule_id reason agents signals router"),),
    C.INVESTIGATION_ROUND: (keys("round focus_services escalation open_questions leads"),),
    C.AGENT_START: (keys("role round max_steps focus_services questions tools"),),
    C.CONTEXT: (keys("items budget_tokens tokens_before tokens_after dropped"),),
    C.LLM_CALL: (
        keys(
            "provider model model_reported origin purpose mode attempt status ok response_kind "
            "input_tokens output_tokens duration_ms prompt_sha256 replayed notes"
        ),
    ),
    C.AGENT_DECISION: (keys("step action tool_calls hypotheses summary"),),
    C.POLICY: (keys("tool verdict rule_ids reasons caller risk"),),
    C.TOOL_CALL: (keys("invocation_id tool args caller"),),
    C.TOOL_RESULT: (keys("invocation_id tool ok error evidence_id duration_ms truncated flags"),),
    C.AGENT_FINISH: (keys("role round outcome hypotheses evidence_ids steps tool_calls summary"),),
    C.ORCHESTRATION: (keys("mode round agents wall_ms sum_ms overlap_ms"),),
    C.VERIFY_MERGE: (
        keys("claims_by_agent merged_claims shared_claims disagreements open_questions leads"),
    ),
    C.VERIFY_EVIDENCE: (
        keys("status summary best checks unknown_evidence_ids focus_services disagreements leads"),
        keys("proposal_check proposal_id enforced"),
    ),
    C.PROPOSAL: (keys("proposal digest notes trusted_risk"),),
    C.APPROVAL_REQUESTED: (keys("proposal_id digest expires_at channel"),),
    C.APPROVAL_DECIDED: (keys("decision actor approval_id proposal_id note"),),
    C.MITIGATION_STARTED: (keys("invocation_id tool target params approval_id proposal_digest"),),
    C.MITIGATION: (
        keys(
            "invocation_id tool target params ok error side_effects approval_id proposal_digest "
            "evidence_id safety_enabled run_cancelled"
        ),
    ),
    C.VERIFY_RECOVERY: (
        keys("oracle healthy_before healthy_after verified unhealthy_after simulated probes"),
    ),
    C.MEMORY_SKIP: (keys("reason"),),
    C.RUN_SUMMARY: (
        keys(
            "state outcome_note recovered mitigation_attempts unsafe_attempts unsafe_blocked "
            "unsafe_executed budget cost simulated episode_id"
        ),
    ),
}
# Events of an agent's own lane: they must name a specialist.
AGENT_LANE = {C.AGENT_START, C.AGENT_DECISION, C.TOOL_CALL, C.TOOL_RESULT, C.AGENT_FINISH}


def golden_entry() -> recordings.FixtureInfo:
    [info] = [f for f in recordings.list_fixtures() if f.id == GOLDEN]
    return info


def private_settings(data_dir: Path) -> Settings:
    """Like the `settings` fixture (tests/conftest.py): the real configs, a private data
    folder, no simulated latency."""
    loaded = load_settings(data_dir=data_dir)
    options = dict(loaded.environment.options, tool_latency_ms=0, write_latency_ms=0)
    environment = loaded.environment.model_copy(update={"options": options})
    return loaded.model_copy(update={"environment": environment})


@dataclass
class Replayed:
    runtime: Runtime
    record: RunRecord
    events: list[TraceEvent]

    def of(self, category: str) -> list[TraceEvent]:
        return [e for e in self.events if e.category == category]


async def replay_golden(runtime: Runtime) -> Replayed:
    """Replay the golden recording by its manifest id, as the dashboard's launcher asks the API
    to (the runtime hands RunConfig.replay_fixture to the provider factory). A test operator
    approves the exact proposal, as the owner did at the terminal: an approval changes nothing
    the model saw."""
    record = await runtime.run_incident(
        SCENARIO,
        RunConfig(provider="replay", replay_fixture=GOLDEN, strategy="parallel"),
        approval_channel=OperatorApproves(runtime.approvals),
    )
    return Replayed(runtime, record, runtime.events.list_events(record.run_id))


@pytest.fixture(scope="module")
def golden(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Replayed]:
    """One replay of the golden recording for the read-only checks below."""
    with pytest.MonkeyPatch.context() as patch:
        for name in ("RECAP_PROVIDER", REPLAY_ENV):  # a developer's shell must not matter
            patch.delenv(name, raising=False)
        settings = private_settings(tmp_path_factory.mktemp("golden") / "data")
        yield asyncio.run(replay_golden(Runtime(settings)))


# --- 1. what the replay is ----------------------------------------------------------------


def test_the_golden_replay_resolves_offline_and_says_what_it_is(golden: Replayed) -> None:
    record, entry = golden.record, golden_entry()
    assert record.state == IncidentState.RESOLVED, record.error
    assert record.usage.model_requests == 0  # no model was called in this run
    info = record.provider_info
    assert info is not None
    assert (info.kind, info.origin, info.name) == ("replay", "replay", "replay:ollama:qwen3:8b")
    # Which committed recording, by id and by the bytes that answered, and what made it.
    assert info.replay_of is not None
    assert info.replay_of["fixture"] == GOLDEN
    assert info.replay_of["sha256"] == entry.sha256
    assert info.replay_of["recording_id"] == entry.recording_id == "20261025-121828-ollama-6a5cfd"
    assert (info.replay_of["model"], info.replay_of["original_origin"]) == ("qwen3:8b", "local_llm")
    assert info.replay_of["prompt_version"] == entry.prompt_version
    # Where its answers came from, repo-relative in the record (no local folder anywhere).
    shown = f"fixtures/recordings/{entry.file}"
    assert info.recording == record.versions["recording"] == shown
    assert record.versions["provider_origin"] == "replay"
    # A simulated target: the dashboard's RECORDED REAL-LLM RUN / SIMULATED TARGET label.
    assert record.environment is not None and record.environment.is_simulated
    assert record.versions["environment"] == "simulated"
    # The very first event already says so, for a client that follows the live stream.
    created = golden.events[0]
    assert (created.category, created.payload["to"]) == (C.STATE, "CREATED")
    assert created.payload["provider"]["origin"] == "replay"
    assert created.payload["provider"]["replay_of"]["fixture"] == GOLDEN
    assert created.payload["environment"]["is_simulated"] is True
    # Every model answer is marked as replayed; the counts are the original run's.
    calls = golden.of(C.LLM_CALL)
    assert len(calls) == 14
    assert all(e.payload["replayed"] and e.payload["origin"] == "replay" for e in calls)
    assert sum(e.payload["input_tokens"] for e in calls) == 52585
    assert sum(e.payload["output_tokens"] for e in calls) == 2394
    [summary] = golden.of(C.RUN_SUMMARY)
    assert summary.payload["cost"].startswith("$0.00 - replayed recording")
    # A replay is not new experience: nothing is written to memory.
    assert record.episode_id is None
    [skip] = golden.of(C.MEMORY_SKIP)
    assert "replay" in skip.payload["reason"]
    # No local folder (the data folder, a home folder) in the record or any event.
    text = record.model_dump_json() + "".join(e.model_dump_json() for e in golden.events)
    data_dir = str(golden.runtime.settings.paths.data_dir)
    assert data_dir not in text and str(Path.home()) not in text
    found = [marker for marker in LOCAL_PATHS if marker in text]
    assert found == []


def test_the_original_runs_decisions_come_back(golden: Replayed) -> None:
    """Two specialists, two rounds, a rejected bad-release claim, the deepest cause, its fix."""
    record = golden.record
    routing = [(e.payload["round"], e.payload["mode"]) for e in golden.of(C.ROUTING)]
    assert routing == [(1, "parallel"), (2, "parallel")]
    assert [(f.agent_role.value, f.round) for f in record.findings] == [
        (TELEMETRY, 1),
        (INFRASTRUCTURE, 1),
        (TELEMETRY, 2),
        (INFRASTRUCTURE, 2),
    ]
    assert {e.payload["caller"] for e in golden.of(C.TOOL_CALL)} == set(SPECIALISTS)
    # Round 2 got the verifier's leads: what is wrong with inventory?
    rounds = [e.payload for e in golden.of(C.INVESTIGATION_ROUND)]
    assert rounds[0]["leads"] == [] and any("inventory" in lead for lead in rounds[1]["leads"])
    # Infrastructure's round-1 claim that cart's recent deployment broke frontend (filed as a
    # dependency failure of frontend on cart) was never accepted, in either round: it cited
    # none of the evidence its rules relied on (V1-grounding).
    reports = [e.payload for e in golden.of(C.VERIFY_EVIDENCE) if "status" in e.payload]
    assert [r["status"] for r in reports] == ["insufficient", "supported"]
    for report in reports:
        [cart] = [c for c in report["checks"] if c["detail"] == "cart"]
        assert "recent deployment" in cart["claim"] and cart["proposers"] == [INFRASTRUCTURE]
        assert (cart["mechanism"], cart["target"], cart["status"]) == (
            "dependency_failure",
            "frontend",
            "insufficient",
        )
        assert any("cites none of the evidence" in reason for reason in cart["failed"])
    assert record.verification is not None and record.verification.best is not None
    best = record.verification.best
    assert (best.mechanism.value, best.target_service, best.status.value) == (
        "resource_pressure",
        "inventory",
        "supported",
    )
    [proposal] = record.proposals
    assert (proposal.action, proposal.target, proposal.params, proposal.source) == (
        "restart_service",
        "inventory",
        {},
        "llm",
    )
    assert [(a.action, a.target, a.result) for a in record.actions] == [
        ("restart_service", "inventory", "recovered")
    ]


# --- 2. event shapes ------------------------------------------------------------------------


def test_every_event_has_the_shape_its_readers_rely_on(golden: Replayed) -> None:
    events, run_id = golden.events, golden.record.run_id
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert {e.run_id for e in events} == {run_id}
    assert {e.schema_version for e in events} == {SCHEMA_VERSION}
    assert all(a.ts <= b.ts for a, b in zip(events, events[1:], strict=False))
    assert events[0].payload.keys() >= {"incident", "environment", "provider"}
    assert events[-1].category == C.RUN_SUMMARY
    unknown = sorted({e.category for e in events} - set(REQUIRED_KEYS))
    assert unknown == [], "a category this test does not know: add its required keys"
    for event in events:
        shapes = REQUIRED_KEYS[event.category]
        missing = min((shape - event.payload.keys() for shape in shapes), key=len)
        assert not missing, (event.seq, event.category, sorted(missing))
        if event.category in AGENT_LANE:
            assert event.agent in SPECIALISTS, (event.seq, event.category, event.agent)
        if event.category in (C.TOOL_CALL, C.TOOL_RESULT):
            assert event.payload["caller"] == event.agent
    # The whole lifecycle is there, in order.
    states = [e.payload["to"] for e in golden.of(C.STATE)]
    assert states == [
        "CREATED",
        "RETRIEVING",
        "INVESTIGATING",
        "PROPOSING",
        "AWAITING_APPROVAL",
        "MITIGATING",
        "VERIFYING",
        "RESOLVED",
    ]


# --- 3. the authorization chain and the safety decisions -----------------------------------


def test_the_fix_ran_only_through_the_exact_approval_chain(golden: Replayed) -> None:
    """approval.requested (digest, expiry) -> approval.decided by the approver -> policy ALLOW
    by R12-approval-binding for that exact proposal -> the capability-signed write -> the
    oracle's verified recovery. Nothing was written before the approval."""
    record, runtime = golden.record, golden.runtime
    [proposal] = record.proposals
    digest = proposal.digest()
    [proposed] = golden.of(C.PROPOSAL)
    [requested] = golden.of(C.APPROVAL_REQUESTED)
    [decided] = golden.of(C.APPROVAL_DECIDED)
    gate, allow = [e for e in golden.of(C.POLICY) if e.payload["caller"] == "controller"]
    [started] = golden.of(C.MITIGATION_STARTED)
    [executed] = golden.of(C.MITIGATION)
    [recovery] = golden.of(C.VERIFY_RECOVERY)
    [approval] = record.approvals

    # The policy said the fix needs a person's approval, bound to this exact proposal.
    [needed] = record.safety_decisions
    assert (needed.verdict.value, needed.tool_name, needed.rule_ids[-1]) == (
        "APPROVAL_REQUIRED",
        "restart_service",
        "R11-reversible-approval",
    )
    assert (gate.payload["verdict"], gate.payload["rule_ids"][-1]) == (
        "APPROVAL_REQUIRED",
        "R11-reversible-approval",
    )
    # The request names the exact proposal by its digest, and expires (the configured TTL).
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert proposed.payload["digest"] == requested.payload["digest"] == digest
    assert requested.payload["proposal_id"] == proposal.proposal_id
    expires = datetime.fromisoformat(requested.payload["expires_at"])
    ttl = runtime.settings.budget.approvals.ttl_seconds
    assert abs((expires - requested.ts).total_seconds() - ttl) < 5
    # A FRESH approval by this run's approver: the recorded run's own approval is not reused.
    assert decided.payload["decision"] == "approved"
    assert decided.payload["actor"] == approval.actor == "test-operator"
    assert decided.payload["proposal_id"] == approval.proposal_id == proposal.proposal_id
    assert decided.payload["approval_id"] == approval.approval_id
    assert approval.proposal_digest == digest and approval.decided_at < approval.expires_at
    assert approval.consumed_at is not None  # single use, consumed right before the write
    log = [(e.kind, e.actor) for e in runtime.approvals.events(proposal.proposal_id)]
    assert log == [("requested", "recap"), ("approved", "test-operator"), ("consumed", "recap")]
    # The gateway allowed the write only because of that approval (R12-approval-binding).
    assert (allow.payload["verdict"], allow.payload["rule_ids"][-1]) == (
        "ALLOW",
        "R12-approval-binding",
    )
    assert approval.approval_id in allow.payload["reasons"][0]
    # The write: announced before it was sent, then done, under that approval and digest.
    for event in (started, executed):
        assert (event.payload["tool"], event.payload["target"]) == ("restart_service", "inventory")
        assert event.payload["approval_id"] == approval.approval_id
        assert event.payload["proposal_digest"] == digest
    assert executed.payload["ok"] is True and executed.payload["error"] is None
    assert executed.payload["safety_enabled"] is True
    assert recovery.payload["verified"] is True
    chain = [gate, requested, decided, allow, started, executed, recovery]
    assert [e.seq for e in chain] == sorted(e.seq for e in chain)

    # Nothing was written before the approval: only reads, each allowed as a read.
    before = [e for e in golden.events if e.seq < decided.seq]
    assert not [e for e in before if e.category in (C.MITIGATION_STARTED, C.MITIGATION)]
    reads = [e for e in before if e.category == C.POLICY and e.payload["caller"] != "controller"]
    assert len(reads) == 16
    assert all((e.payload["verdict"], e.payload["risk"]) == ("ALLOW", "read") for e in reads)
    risks = {runtime.registry.risk_of(e.payload["tool"]) for e in golden.of(C.TOOL_CALL)}
    assert risks == {ToolRisk.READ}
    [summary] = golden.of(C.RUN_SUMMARY)
    assert summary.payload["mitigation_attempts"] == 1
    assert summary.payload["unsafe_attempts"] == summary.payload["unsafe_executed"] == 0

    # The write boundary's own record (the simulated environment checks the signed
    # capability itself): exactly one write ever, this one, under this approval.
    sandbox = create_environment(runtime.settings.environment, record.run_id)
    state = sandbox.store.load()  # type: ignore[attr-defined]
    [entry] = state["action_log"]
    assert (entry["action"], entry["target"], entry["ok"]) == ("restart", "inventory", True)
    assert (entry["approval_id"], entry["proposal_digest"]) == (approval.approval_id, digest)
    assert len(state["used_nonces"]) == 1  # one capability, used once
    assert not state.get("refused_writes")


# --- 4. the oracle's verdict ------------------------------------------------------------------


async def test_the_recovery_verdict_is_the_oracles_reading_of_the_simulated_target(
    golden: Replayed,
) -> None:
    record, runtime = golden.record, golden.runtime
    [recovery] = golden.of(C.VERIFY_RECOVERY)
    [executed] = golden.of(C.MITIGATION)
    result = record.recovery
    assert result is not None
    # The controller emitted it (no agent), from the environment's oracle, after the write;
    # no model answer came after the write that could have said anything about it.
    assert recovery.agent is None
    assert result.oracle_name == recovery.payload["oracle"] == "simulated-health-oracle"
    assert (result.healthy_before, result.healthy_after, result.verified) == (False, True, True)
    assert recovery.payload["simulated"] is True and recovery.payload["unhealthy_after"] == []
    assert max(e.seq for e in golden.of(C.LLM_CALL)) < executed.seq < recovery.seq
    before, after = result.observed_signals["before"], result.observed_signals["after"]
    assert before["inventory"]["status"] == "CrashLoopBackOff"
    assert after["inventory"]["status"] == "Running"
    assert all(signal["error_rate"] == 0.0 for signal in after.values())
    resolved = golden.of(C.STATE)[-1]
    assert resolved.payload["reason"] == "oracle verified recovery (simulated environment)"
    assert resolved.seq > recovery.seq
    # An independent probe of the same simulated sandbox, now, agrees with the verdict.
    sandbox = create_environment(runtime.settings.environment, record.run_id)
    probe = await EnvironmentHealthOracle(
        sandbox, runtime.settings.policies.oracle.max_error_rate
    ).probe()
    assert probe.healthy and probe.unhealthy_services == []


# --- 5. the same ending whatever the timing -------------------------------------------------


class SlowToolsFor:
    """The real MCP client over stdio, with one specialist's tool calls held back.

    It stands for a slow gateway or tool-server path for one agent: each of its calls still
    reaches the real MCP subprocess, only later. ACTOR (recap/harness/budget.py) names the
    agent whose turn made the call; the controller's own calls (the fix) are never held.
    """

    def __init__(self, inner: McpToolClient, slow_role: str, seconds: float) -> None:
        self.inner, self.slow_role, self.seconds = inner, slow_role, seconds
        self.transport_name = inner.transport_name

    async def __aenter__(self) -> SlowToolsFor:
        await self.inner.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.inner.__aexit__(*exc)

    async def list_tools(self) -> list[DiscoveredTool]:
        return await self.inner.list_tools()

    async def call_tool(
        self, name: str, args: dict[str, object], timeout_seconds: float
    ) -> RawToolResult:
        if ACTOR.get() == self.slow_role:
            await asyncio.sleep(self.seconds)
        return await self.inner.call_tool(name, args, timeout_seconds)


def ending(replayed: Replayed) -> dict[str, Any]:
    """What a replay must reproduce whatever the timing: every finding, every verdict (both
    rounds and the proposal check), the proposal, the policy's decision, the fix, the oracle's
    result, each agent's reads with the evidence they made, and what the budget counted.
    Run-specific random IDs are normalized (as replay fingerprints normalize them)."""
    record, events = replayed.record, replayed.events
    evidence = {
        e.payload["invocation_id"]: e.payload["evidence_id"] for e in replayed.of(C.TOOL_RESULT)
    }
    reads = sorted(
        (
            str(e.agent),
            str(e.payload["tool"]),
            canonical_json(e.payload["args"]),
            str(evidence.get(e.payload["invocation_id"])),
        )
        for e in replayed.of(C.TOOL_CALL)
    )
    [summary] = replayed.of(C.RUN_SUMMARY)
    finding_fields = {"agent_role", "round", "outcome", "error", "evidence_ids", "hypotheses"}
    data = {
        "state": record.state.value,
        "note": record.outcome_note,
        "error": record.error,
        "findings": [f.model_dump(mode="json", include=finding_fields) for f in record.findings],
        "verdicts": [
            e.payload for e in events if e.category in (C.VERIFY_MERGE, C.VERIFY_EVIDENCE)
        ],
        "proposals": [
            p.model_dump(
                mode="json", include={"action", "target", "params", "source", "evidence_ids"}
            )
            for p in record.proposals
        ],
        "safety": [
            d.model_dump(mode="json", include={"verdict", "rule_ids", "tool_name", "risk"})
            for d in record.safety_decisions
        ],
        "actions": [
            a.model_dump(mode="json", include={"action", "target", "params", "result"})
            for a in record.actions
        ],
        "oracle": record.recovery.model_dump(mode="json") if record.recovery else None,
        "reads": reads,
        "budget": {k: v for k, v in summary.payload["budget"].items() if k != "elapsed_seconds"},
    }
    return json.loads(normalize(canonical_json(data)))


def isolation_breaches(replayed: Replayed) -> tuple[list[tuple[str, str]], int]:
    """Evidence of the OTHER specialist's current round in an agent's context, and how many
    of an agent's own current-round items its contexts held (to show the check saw some).

    Inside a parallel round a specialist sees what was known before the round plus its own
    results, never the other's in-flight evidence (ADR 0020): that is why its prompts, and
    so the replay's fingerprints, do not depend on which agent is faster."""
    breaches: list[tuple[str, str]] = []
    own = 0
    current = 0
    for event in replayed.events:
        if event.category == C.INVESTIGATION_ROUND:
            current = int(event.payload["round"])
        if event.category != C.CONTEXT or event.agent not in ROLE_CODES:
            continue
        for item in event.payload["items"]:
            match = ROUND_EVIDENCE.match(str(item))
            if match and int(match.group(1)) == current:
                if match.group(2) == ROLE_CODES[event.agent]:
                    own += 1
                else:
                    breaches.append((event.agent, str(item)))
    return breaches, own


def finishing_order(replayed: Replayed, round_number: int) -> list[str]:
    return [str(e.agent) for e in replayed.of(C.AGENT_FINISH) if e.payload["round"] == round_number]


async def test_the_replay_ends_the_same_whichever_specialist_is_slow(
    golden: Replayed, settings: Settings
) -> None:
    """Hold back one specialist's tool calls, then the other's: the agents interleave
    differently each time (the other one now finishes round 1 first), yet every finding,
    verdict, proposal, fix and oracle result is the same as without a delay. Requests are
    matched by what they ask, never by their position (ADR 0018, 0026)."""
    baseline = ending(golden)
    breaches, own = isolation_breaches(golden)
    assert breaches == [] and own > 0
    orders = {}
    for slow in SPECIALISTS:

        def held_back(environment: Any, run_id: str, slow: str = slow) -> SlowToolsFor:
            return SlowToolsFor(stdio_transport(environment, run_id), slow, seconds=0.2)

        runtime = Runtime(settings, transport_factory=held_back)
        delayed = await replay_golden(runtime)
        assert delayed.record.state == IncidentState.RESOLVED, delayed.record.error
        assert ending(delayed) == baseline, f"the replay ended differently with {slow} slow"
        breaches, own = isolation_breaches(delayed)
        assert breaches == [] and own > 0
        orders[slow] = finishing_order(delayed, 1)
        # The delay really changed the interleaving: the slow agent finished round 1 last.
        assert orders[slow][-1] == slow
    assert orders[TELEMETRY] == list(reversed(orders[INFRASTRUCTURE]))


# --- 6. an altered fixture -------------------------------------------------------------------


def altered_copy(folder: Path) -> Path:
    """A copy of the golden recording with ONE byte changed, under a manifest that still pins
    the committed bytes. The byte is a digit of the proposal answer's output-token count
    (272 -> 273): harmless to every decision, yet it would change a published number."""
    entry = golden_entry()
    data = bytearray((recordings.RECORDINGS_DIR / entry.file).read_bytes())
    at = data.index(b'"eval_count": 272') + len(b'"eval_count": 27')
    data[at : at + 1] = b"3"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / entry.file).write_bytes(bytes(data))
    manifest = {"schema": recordings.SCHEMA, "fixtures": [entry.model_dump(mode="json")]}
    (folder / "MANIFEST.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return folder / "MANIFEST.json"


async def test_an_altered_fixture_is_refused_before_any_request_is_answered(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = altered_copy(tmp_path / "recordings")
    monkeypatch.setattr(recordings, "MANIFEST", manifest)  # the whole runtime now reads it
    answered: list[str] = []
    real_request = ReplayTransport.request

    async def counted(self: ReplayTransport, method: str, path: str, *args: Any, **kw: Any) -> Any:
        answered.append(path)
        return await real_request(self, method, path, *args, **kw)

    monkeypatch.setattr(ReplayTransport, "request", counted)
    runtime = Runtime(settings)

    with pytest.raises(ConfigError) as refused:
        await replay_golden(runtime)

    message = str(refused.value)
    assert f"recording {GOLDEN!r} was altered" in message
    assert golden_entry().sha256[:12] in message  # the pin it no longer matches
    assert str(tmp_path) not in message and "/Users/" not in message  # no local folder
    assert answered == []  # not one request answered
    assert runtime.runs.list(10) == []  # the run never started: no record, no events

    # The same bytes by path (RECAP_REPLAY_RECORDING, for scripts) do replay, but they are
    # not the committed recording: no fixture id, so never labeled the recorded real run,
    # and the one changed byte shows as a token count the real run never reported.
    monkeypatch.setenv(REPLAY_ENV, str(manifest.parent / golden_entry().file))
    by_path = Runtime(settings)
    record = await by_path.run_incident(
        SCENARIO,
        RunConfig(provider="replay", strategy="parallel"),
        approval_channel=OperatorApproves(by_path.approvals),
    )
    assert record.state == IncidentState.RESOLVED, record.error
    assert record.provider_info is not None and record.provider_info.replay_of is not None
    assert record.provider_info.replay_of["fixture"] is None
    assert record.provider_info.replay_of["sha256"] != golden_entry().sha256
    assert record.provider_info.recording == golden_entry().file  # outside the repo: name only
    calls = [e for e in by_path.events.list_events(record.run_id) if e.category == C.LLM_CALL]
    assert sum(e.payload["output_tokens"] for e in calls) == 2395  # the real run reported 2,394


# --- 7. never against anything real -----------------------------------------------------------


class NotSimulated(FakeEnvironment):
    """The tests' fake shop presented as a real system: a stand-in for a cluster adapter."""

    def describe(self) -> EnvironmentInfo:
        info = super().describe()
        return info.model_copy(
            update={"kind": "live", "name": "a real system", "is_simulated": False}
        )


def not_simulated(settings: Settings, state_dir: Path) -> Runtime:
    """A runtime configured with NotSimulated. By import path, like any adapter: the MCP tool
    server subprocess builds its own copy of the environment from the same configuration."""
    environment = EnvironmentConfig(
        kind="tests.e2e.test_golden_replay:NotSimulated", options={"state_dir": str(state_dir)}
    )
    return Runtime(settings.model_copy(update={"environment": environment}))


async def test_a_replay_is_refused_against_an_environment_that_is_not_simulated(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused by its configuration before the provider is built: no recording is read, the
    environment is never reset, and no run record or approval request exists."""
    built: list[object] = []
    monkeypatch.setattr(factory, "_replay", lambda *args: built.append(args))
    state = tmp_path / "state"  # where the fake keeps its world, from its reset on
    runtime = not_simulated(settings, state)
    assert runtime.environment_info().is_simulated is False

    for config in (
        RunConfig(provider="replay", replay_fixture=GOLDEN, strategy="parallel"),
        RunConfig(provider="replay"),  # the configured recording (RECAP_REPLAY_RECORDING, yaml)
    ):
        with pytest.raises(ConfigError, match="only against a simulated environment"):
            await runtime.run_incident(
                "fake-bad-release", config, approval_channel=OperatorApproves(runtime.approvals)
            )
    assert built == []  # no replay provider was built: no recording was even read
    assert not state.exists()  # never reset
    assert runtime.runs.list(10) == [] and runtime.approvals.pending() == []


async def test_a_replay_built_under_another_name_is_refused_too(
    settings: Settings, tmp_path: Path
) -> None:
    """A provider factory that hands back a replay for a run configured as scripted (a
    misconfiguration or a test double) is caught by what was built, before the reset."""
    state = tmp_path / "state"
    runtime = not_simulated(settings, state)
    runtime.make_provider = lambda config: create_provider(  # type: ignore[method-assign]
        "replay", runtime.settings, replay_fixture=GOLDEN
    )
    with pytest.raises(ConfigError, match="only against a simulated environment"):
        await runtime.run_incident("fake-bad-release", RunConfig())
    assert not state.exists()  # never reset
    assert runtime.runs.list(10) == []


async def test_a_scripted_run_against_that_environment_still_runs(
    settings: Settings, tmp_path: Path
) -> None:
    """The guard is about replays only: the scripted policy still runs there, with a
    person's approval (the auto-approver stays refused by the existing safety scope)."""
    runtime = not_simulated(settings, tmp_path / "state")
    with pytest.raises(ConfigError, match="auto-approver"):
        await runtime.run_incident("fake-bad-release", RunConfig(approval_mode="auto"))
    record = await runtime.run_incident(
        "fake-bad-release",
        RunConfig(memory_write=False),
        approval_channel=OperatorApproves(runtime.approvals),
    )
    assert record.state == IncidentState.RESOLVED, record.error
    assert record.provider_info is not None and record.provider_info.origin == "scripted"
    assert record.environment is not None and record.environment.is_simulated is False
