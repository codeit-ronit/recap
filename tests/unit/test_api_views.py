"""The API's typed views (apps/api/views.py) say only what a run recorded.

Every test builds records, evidence and trace events by hand, so it can show exactly what a
view does with an old, partial or broken record: it copies, counts and matches recorded
values, marks what is missing as not recorded, and never fills a gap with a guess.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from apps.api.dto import TimelineFilters
from apps.api.service import check_fixture, selection_rule
from apps.api.views import (
    CANCELLED_NOTE,
    CATALOG_FIELDS,
    CONCURRENCY_NOTE,
    REPLAY_NOTE,
    SCRIPTED_NOTE,
    LocalPaths,
    RunFilters,
    batch_summary,
    event_rounds,
    evidence_view,
    file_id,
    model_request,
    retrieval_view,
    run_outcome,
    run_summary,
    timeline_entry,
    timeline_view,
    usage_view,
)
from evals.records import EvalBatch, EvalRecord
from recap.core.config import ProviderSettings, Settings
from recap.core.models import (
    ActionRecord,
    AgentFinding,
    AgentRole,
    ApprovalRequest,
    DocChunk,
    EnvironmentInfo,
    EpisodeOutcome,
    Evidence,
    ExperienceEpisode,
    FailureMechanism,
    Hypothesis,
    HypothesisCheck,
    HypothesisStatus,
    IncidentState,
    MemoryHit,
    MemoryPartition,
    MitigationProposal,
    ProviderInfo,
    RecoveryResult,
    RetrievedChunk,
    RoutingSignals,
    RunConfig,
    RunRecord,
    SourceType,
    StateChange,
    StrategyDecision,
    StrategyMode,
    TraceEvent,
    TrustLevel,
    Usage,
    VerificationReport,
)
from recap.providers.recordings import FixtureInfo
from recap.telemetry.tracer import TraceCategory as C

T0 = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)
SIMULATED = EnvironmentInfo(kind="simulated", name="shop", namespace="shop", is_simulated=True)
SCRIPTED = ProviderInfo(name="deterministic-scripted-policy", kind="scripted", origin="scripted")
LOCAL = ProviderInfo(
    name="ollama:qwen3:8b",
    kind="llm",
    origin="local_llm",
    model="qwen3:8b",
    mode="tools",
    prompt_version="prompts-v4.7",
    recording="data/recordings/20261025-121828-ollama-6a5cfd.jsonl",
)
GOLDEN = FixtureInfo(
    id="qwen3-8b-unfamiliar-parallel",
    file="ollama-qwen3-8b-unfamiliar-parallel.jsonl",
    sha256="ab" * 32,
    bytes=10,
    recording_id="20261025-121828-ollama-6a5cfd",
    title="Unfamiliar incident",
    scenario="m3-inventory-leak-behind-frontend",
    strategy="parallel",
    use_memory=True,
    provider="ollama",
    model="qwen3:8b",
    prompt_version="prompts-v4.7",
    approved_by="the owner",
    exported_run="evals/live/2026-10-25-qwen3-8b/",
)


def record(state: IncidentState = IncidentState.RESOLVED, **fields: Any) -> RunRecord:
    """A finished scripted run that went CREATED -> ... -> `state` in 90 seconds."""
    history = [
        StateChange(state=IncidentState.CREATED, at=T0),
        StateChange(state=IncidentState.INVESTIGATING, at=T0 + timedelta(seconds=1)),
    ]
    if state != IncidentState.INVESTIGATING:
        history.append(StateChange(state=state, at=T0 + timedelta(seconds=90)))
    values: dict[str, Any] = {
        "run_id": "run-0000000001",
        "scenario_id": "bad-deployment",
        "config": RunConfig(),
        "state": state,
        "history": history,
        "environment": SIMULATED,
        "provider": SCRIPTED.name,
        "provider_info": SCRIPTED,
        "versions": {"config_hash": "cfg", "code_version": "abc123", "recap": "0.4.0"},
        "created_at": T0,
        "updated_at": T0 + timedelta(seconds=90),
    }
    return RunRecord(**(values | fields))


def event(seq: int, category: str, payload: dict[str, Any], **fields: Any) -> TraceEvent:
    values: dict[str, Any] = {
        "event_id": f"evt-{seq}",
        "run_id": "run-0000000001",
        "seq": seq,
        "stage": "INVESTIGATING",
        "ts": T0 + timedelta(seconds=seq),
        "category": category,
        "payload": payload,
    }
    return TraceEvent(**(values | fields))


def llm_call(seq: int, agent: str, **payload: Any) -> TraceEvent:
    body = {
        "step": 1,
        "provider": "ollama",
        "model": "qwen3:8b",
        "origin": "local_llm",
        "purpose": "investigate",
        "mode": "tools",
        "attempt": 1,
        "status": 200,
        "ok": True,
        "input_tokens": 1000,
        "output_tokens": 50,
        "duration_ms": 20000.0,
        "replayed": False,
    }
    return event(seq, C.LLM_CALL, body | payload, agent=agent)


def finding(role: AgentRole, round_: int, *hypotheses: Hypothesis, **fields: Any) -> AgentFinding:
    values: dict[str, Any] = {
        "agent_role": role,
        "round": round_,
        "hypotheses": list(hypotheses),
        "started_at": T0 + timedelta(seconds=2),
        "finished_at": T0 + timedelta(seconds=12),
        "outcome": "hypothesis" if hypotheses else "need_more_evidence",
    }
    return AgentFinding(**(values | fields))


def tool_evidence(evidence_id: str, **fields: Any) -> Evidence:
    values: dict[str, Any] = {
        "id": evidence_id,
        "incident_id": "inc-1",
        "source_type": SourceType.TOOL,
        "source_ref": "mcp:get_service_logs(service=checkout)",
        "timestamp": T0,
        "content": "ERROR boom",
        "tool_name": "get_service_logs",
        "agent_role": AgentRole.TELEMETRY,
    }
    return Evidence(**(values | fields))


# --- outcome and summary ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "fields", "expected"),
    [
        (IncidentState.INVESTIGATING, {}, "in_progress"),
        (IncidentState.RESOLVED, {}, "resolved"),
        (IncidentState.BUDGET_EXHAUSTED, {}, "budget_exhausted"),
        (IncidentState.QUOTA_EXHAUSTED, {}, "quota_deferred"),
        (IncidentState.BLOCKED, {"result": "rejected"}, "approval_rejected"),
        (IncidentState.BLOCKED, {"result": "expired"}, "approval_expired"),
        (IncidentState.BLOCKED, {"result": "denied"}, "safety_blocked"),
        (IncidentState.BLOCKED, {}, "safety_blocked"),  # the namespace allowlist: no action
        (IncidentState.FAILED, {"note": CANCELLED_NOTE}, "cancelled"),
        (IncidentState.FAILED, {"error": "ProviderError: down"}, "error"),
        (IncidentState.FAILED, {"note": "evidence stayed insufficient"}, "failed"),
    ],
)
def test_the_outcome_comes_from_the_recorded_state_action_and_note(
    state: IncidentState, fields: dict[str, str], expected: str
) -> None:
    actions = []
    if "result" in fields:
        actions = [ActionRecord(action="restart_service", target="cart", result=fields["result"])]
    run = record(
        state, actions=actions, outcome_note=fields.get("note", ""), error=fields.get("error")
    )
    assert run_outcome(run) == expected


def test_a_finished_run_summary_has_its_times_oracle_and_labels() -> None:
    recovery = RecoveryResult(
        healthy_before=False,
        healthy_after=True,
        oracle_name="simulated-health-oracle",
        verified=True,
        checked_at=T0,
    )
    run = record(
        recovery=recovery,
        versions={
            "config_hash": "cfg",
            "code_version": "c",
            "recap": "r",
            "prompt_version": "prompts-v3.3",
        },
    )

    summary = run_summary(run, "finished")

    assert (summary.started_at, summary.ended_at) == (T0, T0 + timedelta(seconds=90))
    assert summary.wall_seconds == 90.0 and summary.terminal and summary.outcome == "resolved"
    assert summary.oracle is not None and summary.oracle.verified and summary.recovered
    assert summary.labels == ["SIMULATED ENVIRONMENT", "SCRIPTED BASELINE (NOT AN LLM)"]
    # The scripted policy uses no prompts, whatever the record's historical default says.
    assert summary.prompt_version is None and summary.model is None
    assert summary.not_recorded == []


def test_a_running_run_has_no_end_and_no_oracle_verdict_yet() -> None:
    summary = run_summary(record(IncidentState.INVESTIGATING), "running")

    assert summary.ended_at is None and summary.wall_seconds is None
    assert not summary.terminal and summary.outcome == "in_progress"
    assert summary.oracle is None


def test_an_old_record_is_summarized_with_its_gaps_named_not_filled() -> None:
    old = RunRecord(run_id="run-old", scenario_id="bad-deployment", config=RunConfig())

    summary = run_summary(old, "external")

    assert summary.provider_class is None and summary.environment_simulated is None
    assert summary.labels == []  # nothing claims SIMULATED without a recorded environment
    assert summary.not_recorded == [
        "provider_info",
        "versions.config_hash",
        "versions.code_version",
        "versions.recap",
        "environment",
        "history",
    ]


def test_a_live_model_run_names_its_own_recording_by_id_only() -> None:
    summary = run_summary(record(provider=LOCAL.name, provider_info=LOCAL), "finished")

    assert summary.recording == "20261025-121828-ollama-6a5cfd"
    assert summary.labels == ["SIMULATED ENVIRONMENT", "LIVE LOCAL MODEL"]
    assert summary.replay is None and summary.model == "qwen3:8b"


def replay_info(**replay_of: Any) -> ProviderInfo:
    header = {
        "recording_id": GOLDEN.recording_id,
        "original_provider": "ollama",
        "model": "qwen3:8b",
        "created_at": "2026-10-25T12:18:28+00:00",
        "prompt_version": "prompts-v4.7",
    }
    return LOCAL.model_copy(
        update={
            "name": "replay:ollama:qwen3:8b",
            "kind": "replay",
            "origin": "replay",
            "replay_of": header | replay_of,
            "recording": "fixtures/recordings/ollama-qwen3-8b-unfamiliar-parallel.jsonl",
        }
    )


def test_a_replay_of_a_committed_fixture_says_so_and_links_the_original_export() -> None:
    info = replay_info(fixture=GOLDEN.id, sha256="cd" * 32)
    run = record(provider_info=info, config=RunConfig(provider="replay", replay_fixture=GOLDEN.id))

    summary = run_summary(run, "finished", fixtures=[GOLDEN])

    replay = summary.replay
    assert replay is not None
    assert (replay.fixture_id, replay.recording_id) == (GOLDEN.id, GOLDEN.recording_id)
    assert replay.committed_fixture and replay.recording_matches_fixture
    assert replay.fixture_sha256 == "cd" * 6  # the replayed bytes' own hash, as recorded
    assert replay.original_export == GOLDEN.exported_run
    assert summary.labels == ["SIMULATED ENVIRONMENT", "REPLAY", "RECORDED REAL-LLM RUN"]
    assert summary.recording is None  # a replay records nothing of its own


def test_a_replay_of_any_other_recording_is_never_called_a_real_model_run() -> None:
    info = replay_info(fixture=None, sha256="ef" * 32)  # a test's fake-server recording, say
    run = record(provider_info=info, config=RunConfig(provider="replay"))

    summary = run_summary(run, "finished", fixtures=[GOLDEN])

    assert summary.replay is not None and not summary.replay.committed_fixture
    assert summary.labels == ["SIMULATED ENVIRONMENT", "REPLAY"]


def test_a_replay_that_ran_another_file_than_its_fixture_is_flagged() -> None:
    info = replay_info(fixture=None, sha256="ef" * 32)
    run = record(provider_info=info, config=RunConfig(provider="replay", replay_fixture=GOLDEN.id))

    replay = run_summary(run, "finished", fixtures=[GOLDEN]).replay

    assert replay is not None and replay.recording_matches_fixture is False


def test_an_older_replay_record_is_matched_to_its_fixture_by_recording_id() -> None:
    run = record(provider_info=replay_info(), config=RunConfig(provider="replay"))

    replay = run_summary(run, "finished", fixtures=[GOLDEN]).replay

    assert replay is not None and replay.committed_fixture
    assert replay.fixture_sha256 == GOLDEN.sha256[:12]


def test_run_filters_match_exactly() -> None:
    run = record(provider=LOCAL.name, provider_info=LOCAL, config=RunConfig(label="night"))

    assert RunFilters().match(run)
    assert RunFilters(provider_class="local_llm", provider="qwen3:8b").match(run)
    assert RunFilters(provider="ollama:qwen3:8b", state="RESOLVED", environment="simulated").match(
        run
    )
    assert RunFilters(label="night", scenario="bad-deployment").match(run)
    assert not RunFilters(provider_class="replay").match(run)
    assert not RunFilters(label="nigh").match(run)
    assert not RunFilters(state="FAILED").match(run)


# --- timeline ---------------------------------------------------------------------------------


def routing(seq: int, round_: int, mode: str = "parallel") -> TraceEvent:
    agents = ["telemetry_investigator", "infrastructure_investigator"]
    payload = {"round": round_, "mode": mode, "rule_id": "RT0-forced", "reason": "forced"}
    return event(seq, C.ROUTING, payload | {"agents": agents})


def parallel_trace() -> list[TraceEvent]:
    """Round 1 with two specialists, a proposal, then a new epoch (round 2)."""
    return [
        event(1, C.STATE, {"from": None, "to": "CREATED", "incident": {}}, stage="CREATED"),
        event(2, C.STATE, {"from": "CREATED", "to": "INVESTIGATING"}),
        routing(3, 1),
        event(4, C.INVESTIGATION_ROUND, {"round": 1, "leads": [], "escalation": False}),
        event(
            5,
            C.AGENT_START,
            {"role": "telemetry_investigator", "round": 1},
            agent="telemetry_investigator",
        ),
        event(
            6,
            C.AGENT_START,
            {"role": "infrastructure_investigator", "round": 1},
            agent="infrastructure_investigator",
        ),
        llm_call(7, "telemetry_investigator", server_ms=8000.0),
        llm_call(
            8,
            "infrastructure_investigator",
            attempt=2,
            ok=False,
            input_tokens=None,
            output_tokens=None,
        ),
        event(
            9,
            C.TOOL_CALL,
            {"tool": "get_service_logs", "invocation_id": "call-1"},
            agent="telemetry_investigator",
        ),
        event(
            10,
            C.TOOL_RESULT,
            {
                "tool": "get_service_logs",
                "invocation_id": "call-1",
                "ok": True,
                "evidence_id": "ev-1-r1t01",
            },
            agent="telemetry_investigator",
        ),
        event(
            11,
            C.ORCHESTRATION,
            {
                "mode": "parallel",
                "round": 1,
                "agents": [],
                "wall_ms": 30000.0,
                "sum_ms": 50000.0,
                "overlap_ms": 20000.0,
            },
        ),
        event(12, C.VERIFY_EVIDENCE, {"status": "supported", "checks": []}),
        event(13, C.STATE, {"from": "INVESTIGATING", "to": "PROPOSING"}, stage="PROPOSING"),
        event(
            14,
            C.MITIGATION,
            {"tool": "restart_service", "ok": True, "evidence_id": "ev-1-009"},
            stage="MITIGATING",
        ),
        event(15, C.STATE, {"from": "VERIFYING", "to": "INVESTIGATING"}),
        routing(16, 2, mode="single"),
        event(17, C.AGENT_START, {"role": "investigator", "round": 2}, agent="investigator"),
    ]


def test_events_belong_to_the_round_in_progress_and_none_outside_investigation() -> None:
    rounds = event_rounds(parallel_trace())

    assert [rounds[s] for s in (1, 2, 3, 7, 10, 12)] == [None, None, 1, 1, 1, 1]
    assert [rounds[s] for s in (13, 14)] == [None, None]  # proposing, mitigating
    assert [rounds[s] for s in (15, 16, 17)] == [None, 2, 2]  # a new epoch's own round


def test_a_timeline_page_resumes_after_a_seq_without_repeating_any_event() -> None:
    trace = parallel_trace()
    seen: list[int] = []
    after = 0
    while True:
        page = timeline_view("run-1", None, trace, "finished", after_seq=after, limit=4)
        seen += [entry.seq for entry in page.entries]
        after = page.next_after_seq
        if not page.has_more:
            break
    assert seen == list(range(1, 18))
    assert page.total_events == page.matching_events == 17


def test_timeline_filters_apply_before_paging() -> None:
    trace = parallel_trace()

    by_agent = timeline_view(
        "run-1", None, trace, "finished", filters=TimelineFilters(agent="telemetry_investigator")
    )
    by_round = timeline_view("run-1", None, trace, "finished", filters=TimelineFilters(round=2))
    by_tool = timeline_view(
        "run-1",
        None,
        trace,
        "finished",
        filters=TimelineFilters(kind="tool_result", tool="get_service_logs"),
    )

    assert [e.seq for e in by_agent.entries] == [5, 7, 9, 10]
    assert [e.seq for e in by_round.entries] == [16, 17]
    assert [e.seq for e in by_tool.entries] == [10] and by_tool.entries[0].evidence_ids == [
        "ev-1-r1t01"
    ]


def test_the_timeline_counts_retries_rounds_and_says_requests_can_queue() -> None:
    view = timeline_view(
        "run-1", None, parallel_trace(), "finished", local_model_providers={"ollama"}
    )

    assert view.retries.model_dump() == {
        "model_retries": 1,
        "failed_model_requests": 1,
        "fix_attempts": 1,
    }
    first = view.rounds[0]
    assert (first.mode, first.overlap_ms, first.verification_status) == (
        "parallel",
        20000.0,
        "supported",
    )
    assert first.model_request_ms == 40000.0
    assert CONCURRENCY_NOTE in view.notes
    # A request whose server reported its own time also shows the time outside the server.
    call = view.entries[6].model_request
    assert call is not None and (call.server_ms, call.outside_server_ms) == (8000.0, 12000.0)
    retry = view.entries[7].model_request
    assert retry is not None and retry.retry and retry.input_tokens is None


def test_lanes_come_from_findings_and_unfinished_turns_from_their_start() -> None:
    hypothesis = Hypothesis(claim="x", mechanism=FailureMechanism.RESOURCE_PRESSURE)
    usage = Usage(steps=3, tool_calls=2, model_requests=3, actual_input_tokens=900)
    run = record(
        IncidentState.FAILED,
        outcome_note=CANCELLED_NOTE,
        findings=[
            finding(AgentRole.TELEMETRY, 1, hypothesis, usage=usage),
            finding(AgentRole.INFRASTRUCTURE, 1, error="timeout after 30s", outcome="error"),
        ],
    )

    finished = timeline_view("run-1", run, parallel_trace(), "finished")
    live = timeline_view("run-1", run, parallel_trace(), "running")

    lanes = {(lane.agent, lane.round): lane for lane in finished.lanes}
    telemetry = lanes[("telemetry_investigator", 1)]
    assert (telemetry.steps, telemetry.input_tokens, telemetry.duration_ms) == (3, 900, 10000.0)
    assert telemetry.model_request_ms == 20000.0 and telemetry.model_server_ms == 8000.0
    assert lanes[("infrastructure_investigator", 1)].error == "timeout after 30s"
    # Round 2's investigator started but has no finding: the run ended (or still runs).
    assert lanes[("investigator", 2)].outcome == "unfinished"
    assert lanes[("investigator", 2)].finished_at is None
    assert {(lane.agent, lane.outcome) for lane in live.lanes if lane.round == 2} == {
        ("investigator", "running")
    }


def test_a_record_without_trace_events_says_so() -> None:
    run = record(
        strategies=[
            StrategyDecision(
                mode=StrategyMode.SINGLE,
                reason="clear",
                rule_id="RT7-default",
                selected_agents=[AgentRole.INVESTIGATOR],
                provider="p",
                signals=RoutingSignals(
                    services_affected=1,
                    remaining_tool_calls=30,
                    remaining_steps=30,
                    max_parallel_agents=2,
                ),
            )
        ]
    )

    view = timeline_view("run-1", run, [], "finished")

    assert not view.trace_available and view.entries == [] and view.total_events == 0
    assert view.not_recorded == ["trace events"]
    assert [(r.round, r.rule_id) for r in view.rounds] == [(1, "RT7-default")]  # from the record


def test_a_malformed_event_payload_becomes_empty_fields_not_an_error() -> None:
    broken = event(1, C.LLM_CALL, {"origin": "somewhere", "duration_ms": "slow", "step": "two"})

    entry = timeline_entry(broken, None)

    assert entry.model_request is None and entry.step is None and entry.untrusted_text
    assert model_request({"origin": "local_llm"}) is None  # no recorded duration


# --- evidence ---------------------------------------------------------------------------------


def test_evidence_items_carry_their_trust_class_and_the_claims_that_cite_them() -> None:
    supported = Hypothesis(
        claim="inventory runs out of memory",
        mechanism=FailureMechanism.RESOURCE_PRESSURE,
        target_service="inventory",
        supporting_evidence_ids=["ev-1-001", "ev-1-404"],
        contradicting_evidence_ids=["ev-1-002"],
    )
    check = HypothesisCheck(
        hypothesis=supported.model_copy(update={"status": HypothesisStatus.SUPPORTED}),
        basis_evidence_ids=["ev-1-001"],
        passed=["inventory was OOMKilled 3 times"],
    )
    run = record(
        provider=LOCAL.name,
        provider_info=LOCAL,
        findings=[finding(AgentRole.TELEMETRY, 1, supported)],
        verification=VerificationReport(status="supported", best=supported, checks=[check]),
    )
    registry = [
        tool_evidence("ev-1-001"),
        tool_evidence("ev-1-002", source_type=SourceType.DOC, tool_name="search_runbooks"),
        tool_evidence("ev-1-003", source_type=SourceType.MEMORY),
        tool_evidence("ev-1-004", source_type=SourceType.ALERT),
        tool_evidence("ev-1-005", trust_level=TrustLevel.TRUSTED),
    ]

    view = evidence_view("run-1", run, registry, [], "finished")

    classes = {item.id: item.trust_class for item in view.registry}
    assert classes == {
        "ev-1-001": "untrusted_tool_output",
        "ev-1-002": "untrusted_runbook_text",
        "ev-1-003": "untrusted_memory_text",
        "ev-1-004": "untrusted_alert_text",
        "ev-1-005": "recap_trusted_text",
    }
    first, second = view.registry[0], view.registry[1]
    assert first.supports == ["r1.telemetry_investigator.1"] and first.in_rule_basis
    assert second.contradicts == ["r1.telemetry_investigator.1"] and not second.in_rule_basis
    claim = view.findings[0].claims[0]
    assert claim.author == "model" and claim.unknown_evidence_ids == ["ev-1-404"]
    assert view.unknown_cited_ids == ["ev-1-404"]  # cited, never observed in this run
    final = view.final_report
    assert final is not None and final.source == "run_record"
    assert final.checks[0].supporting_evidence_ids == ["ev-1-001", "ev-1-404"]
    assert view.not_recorded == ["trace events (verification history, proposal checks, flags)"]


def test_verdicts_proposal_checks_and_injection_flags_come_from_the_trace() -> None:
    proposal = MitigationProposal(
        proposal_id="prop-1",
        incident_id="inc-1",
        action="restart_service",
        target="inventory",
        rationale="the model's words",
        evidence_ids=["ev-1-001"],
    )
    run = record(proposals=[proposal])
    checks = [
        {
            "claim": "bad cart release",
            "mechanism": "bad_deployment",
            "target": "cart",
            "status": "insufficient",
            "proposers": ["infrastructure_investigator"],
            "basis_evidence_ids": [],
            "failed": ["cites none of the evidence its rules used"],
        }
    ]
    trace = [
        routing(1, 1),
        event(
            2,
            C.INJECTION,
            {
                "evidence_id": "ev-1-001",
                "tool": "get_service_logs",
                "patterns": ["system-override"],
            },
        ),
        event(
            3,
            C.VERIFY_EVIDENCE,
            {
                "status": "insufficient",
                "summary": "No actionable root cause",
                "checks": checks,
                "disagreements": [
                    {"kind": "not-a-kind"},
                    {"kind": "missing_view", "resolved": False},
                ],
            },
        ),
        event(
            4,
            C.VERIFY_EVIDENCE,
            {
                "proposal_id": "prop-1",
                "proposal_check": {"ok": False, "problems": ["VP2"], "rule_ids": ["VP2-target"]},
                "enforced": True,
            },
            stage="PROPOSING",
        ),
    ]

    view = evidence_view("run-1", run, [tool_evidence("ev-1-001")], trace, "finished")

    [verdict] = view.verifications
    assert (verdict.round, verdict.status) == (1, "insufficient")
    assert verdict.checks[0].failed == ["cites none of the evidence its rules used"]
    assert [d.kind for d in verdict.disagreements] == ["missing_view"]  # the broken one is left out
    assert view.proposal_checks[0].problems == ["VP2"] and not view.proposal_checks[0].ok
    assert view.injection_flags[0].patterns == ["system-override"]
    assert view.proposals[0].digest == proposal.digest()
    assert view.proposals[0].author == "scripted_policy"


# --- retrieval --------------------------------------------------------------------------------


def test_retrieval_says_who_wrote_each_query_and_which_results_were_cited() -> None:
    chunk = DocChunk(
        chunk_id="runbook-memory#remediation-1",
        doc_path="fixtures/knowledge/runbook-memory-pressure.md",
        title="Memory",
        heading="Remediation",
        text="restart",
        checksum="abc",
    )
    hit = MemoryHit(
        episode=ExperienceEpisode(
            episode_id="ep-1",
            origin_run_id="run-0",
            scenario_id="s",
            failure_family="f",
            partition=MemoryPartition.LIVE,
            service="inventory",
            symptoms=[],
            outcome=EpisodeOutcome.SUCCESS,
            created_at=T0,
        ),
        similarity=0.4,
        outcome=EpisodeOutcome.SUCCESS,
    )
    proposal = MitigationProposal(
        proposal_id="prop-1",
        incident_id="inc-1",
        action="restart_service",
        target="inventory",
        rationale="r",
        citations=[chunk.chunk_id],
    )
    run = record(
        provider=LOCAL.name,
        provider_info=LOCAL,
        findings=[finding(AgentRole.TELEMETRY, 1, doc_refs=[chunk.chunk_id])],
        proposals=[proposal],
        retrieved_docs=[RetrievedChunk(chunk=chunk, score=3.21, rank=1, retriever="bm25")],
        memory_hits=[hit],
    )
    result = {
        "chunk_id": chunk.chunk_id,
        "rank": 1,
        "score": 3.2,
        "signals": {"bm25": 3.2, "odd": "x"},
    }
    trace = [
        event(
            1,
            C.RETRIEVAL,
            {"stage": "initial", "query": "inventory oom", "mode": "bm25", "results": [result]},
            stage="RETRIEVING",
        ),
        event(
            2,
            C.MEMORY_SEARCH,
            {
                "readable_partitions": ["train", "live"],
                "hits": [{"episode_id": "ep-1", "similarity": 0.4}],
            },
            stage="RETRIEVING",
        ),
        event(
            3,
            C.RETRIEVAL,
            {
                "stage": "agent",
                "query": "why oom",
                "mode": "hybrid",
                "embedding": {"model": "nomic-embed-text", "duration_ms": 41.5, "replayed": False},
                "results": [],
            },
            agent="telemetry_investigator",
        ),
        event(
            4,
            C.TOOL_RESULT,
            {
                "tool": "search_runbooks",
                "ok": False,
                "error": "the runbook search budget is used up",
            },
            agent="infrastructure_investigator",
        ),
    ]

    view = retrieval_view("run-1", run, trace, "finished")

    initial, agent = view.searches
    assert (initial.query_author, agent.query_author) == ("recap", "model")
    assert initial.results[0].cited_by_agents == ["telemetry_investigator"]
    assert initial.results[0].cited_by_proposal and initial.results[0].signals == {"bm25": 3.2}
    assert agent.embedding is not None and agent.embedding.duration_ms == 41.5
    assert view.agent_search_refusals[0].error == "the runbook search budget is used up"
    assert view.memory.searches[0].hits[0].episode_id == "ep-1"
    # A hit never checked against live evidence has no reuse verdict yet (not "unsafe").
    assert view.memory.final_hits[0].safe_to_reuse is None
    assert view.not_recorded == [
        "search duration (only a hybrid search's embedding request is timed)",
        "retrieval info (mode, embedder, embedding requests)",
    ]


# --- usage ------------------------------------------------------------------------------------


def test_usage_adds_up_reported_tokens_and_time_per_model_and_never_guesses() -> None:
    run = record(
        provider=LOCAL.name, provider_info=LOCAL, strategies=[], usage=Usage(estimated_tokens=4321)
    )
    trace = [
        routing(1, 1),
        llm_call(2, "telemetry_investigator", server_ms=15000.0),
        llm_call(3, "infrastructure_investigator", input_tokens=None, output_tokens=None),
        llm_call(4, "infrastructure_investigator", purpose="propose"),
        event(
            5,
            C.RUN_SUMMARY,
            {"budget": {"model_tokens": 2100}, "cost": "$0.00 - local model"},
            stage="RESOLVED",
        ),
    ]
    spec = ProviderSettings(
        kind="llm",
        origin="local_llm",
        api="ollama",
        base_url="http://127.0.0.1:11434",
        model="qwen3:8b",
    )

    view = usage_view(
        "run-1",
        run,
        trace,
        "finished",
        providers={"ollama": spec},
        local_model_providers={"ollama"},
    )

    [model] = view.models
    assert (model.requests, model.input_tokens, model.output_tokens) == (3, 2000, 100)
    assert model.requests_without_token_counts == 1
    assert (model.request_ms, model.server_ms, model.outside_server_ms) == (
        60000.0,
        15000.0,
        5000.0,
    )
    assert model.requests_with_server_ms == 1
    assert model.by_purpose == {"investigate": 2, "propose": 1}
    assert model.limits is not None and model.limits.max_output_tokens == 1024
    assert view.estimated_context_tokens.estimate is True
    assert view.estimated_context_tokens.tokens == 4321
    assert view.cost_label == "$0.00 - local model" and view.budget.used == {"model_tokens": 2100}
    assert CONCURRENCY_NOTE in view.notes  # a parallel round on one local Ollama server
    assert "usd" not in view.model_dump_json().lower()  # no dollar figure of RECAP's own


def test_a_replay_reports_recorded_counts_without_current_limits() -> None:
    trace = [llm_call(1, "investigator", origin="replay", replayed=True)]

    view = usage_view("run-1", record(provider_info=replay_info()), trace, "finished")

    assert view.models[0].replayed_requests == 1 and view.models[0].limits is None
    assert REPLAY_NOTE in view.notes
    assert "server_ms (model server time)" in view.not_recorded
    assert "run summary (budget used, cost label)" in view.not_recorded


def test_a_scripted_run_has_no_model_usage_and_says_why() -> None:
    view = usage_view("run-1", record(), [], "finished")

    assert view.models == [] and view.notes == [SCRIPTED_NOTE]
    assert view.totals.input_tokens is None  # not zero: nothing was reported


# --- local paths, providers, fixtures, batches -----------------------------------------------


def test_local_folders_never_leave_the_server(tmp_path: Path) -> None:
    root = tmp_path / "RECAP"
    data = tmp_path / "scratch" / "data"
    home = Path("/Users/someone")
    paths = LocalPaths.of(root, data, home=home)

    scrubbed = paths.scrub(
        {
            "recording": f"{data}/recordings/x.jsonl",
            "doc": f"{root}/fixtures/knowledge/a.md",
            "error": f"not found: {home}/keys and /home/other/x and /private/var/folders/ab/cd/T/y",
            "list": [f"{root}", 3, None],
            "plain": "checkout v2.4.1",
        }
    )

    assert scrubbed == {
        "recording": "data/recordings/x.jsonl",
        "doc": "fixtures/knowledge/a.md",
        "error": "not found: ~/keys and ~/x and <tmp>/y",
        "list": [".", 3, None],
        "plain": "checkout v2.4.1",
    }
    assert file_id(f"{data}/recordings/20261025-1.jsonl") == "20261025-1"


async def test_every_catalog_field_is_part_of_the_provider_view(settings: Settings) -> None:
    from recap.providers.catalog import provider_status
    from recap.providers.quota import UsageLedger
    from tests.llm_fakes import FakeOllama

    entries = await provider_status(
        settings,
        UsageLedger(settings.paths.usage_ledger),
        environ={"GROQ_API_KEY": "x" * 20, "RECAP_GROQ_FREE_PLAN_CONFIRMED_ON": "2026-10-20"},
        mock_http=FakeOllama().mock(),
    )

    for entry in entries:
        assert set(entry) <= set(CATALOG_FIELDS), entry["name"]


def test_who_may_pick_which_provider_for_one_run(settings: Settings) -> None:
    def rule(name: str, fixtures: int = 1, config: Settings = settings) -> bool:
        return selection_rule(config, name, launchable_fixtures=fixtures)[0]

    assert rule("deterministic")  # the server's default
    assert rule("ollama")  # api_selectable in configs/providers.yaml (local, $0)
    assert not rule("groq")  # a free quota is spent only if the owner allows it
    assert not rule("anthropic") and not rule("no-such-provider")
    assert rule("replay") and not rule("replay", fixtures=0)
    # A paid provider is never selectable, even with the flag forced past validation.
    providers = dict(settings.providers.providers)
    providers["anthropic"] = providers["anthropic"].model_copy(update={"api_selectable": True})
    forced = settings.model_copy(
        update={"providers": settings.providers.model_copy(update={"providers": providers})}
    )
    assert not rule("anthropic", config=forced)
    # The scripted baseline stays selectable when another provider is the default.
    other = settings.model_copy(
        update={"providers": settings.providers.model_copy(update={"default": "ollama"})}
    )
    assert rule("deterministic", config=other) and rule("ollama", config=other)


def test_a_paid_provider_marked_selectable_is_refused_at_load() -> None:
    with pytest.raises(ValueError, match="paid provider can never be selectable"):
        ProviderSettings(kind="llm", origin="paid_llm", model="m", api_selectable=True)


def test_a_fixture_is_launchable_only_intact_current_and_known(tmp_path: Path) -> None:
    (tmp_path / GOLDEN.file).write_bytes(b"recorded bytes")
    from recap.providers.recordings import sha256_of

    pinned = GOLDEN.model_copy(update={"sha256": sha256_of(tmp_path / GOLDEN.file)})
    scenarios = {GOLDEN.scenario}

    ok = check_fixture(pinned, tmp_path, scenarios, "prompts-v4.7")
    altered = check_fixture(GOLDEN, tmp_path, scenarios, "prompts-v4.7")
    stale = check_fixture(pinned, tmp_path, scenarios, "prompts-v4.8")
    unknown = check_fixture(pinned, tmp_path, set(), "prompts-v4.7")
    missing = check_fixture(
        pinned.model_copy(update={"file": "gone.jsonl"}), tmp_path, scenarios, "prompts-v4.7"
    )

    assert ok.launchable and ok.integrity == "intact"
    assert altered.integrity == "altered" and not altered.launchable
    assert not stale.launchable and not stale.prompt_version_matches
    assert not unknown.launchable and not unknown.scenario_available
    assert missing.integrity == "missing"


def test_a_batch_counts_only_oracle_verified_successes() -> None:
    def rec(run_id: str, **fields: Any) -> EvalRecord:
        return EvalRecord(run_id=run_id, mode="real_llm", scenario_id="s", **fields)

    batch = EvalBatch(
        batch_id="b1",
        mode="real_llm",
        title="t",
        created_at=T0,
        records=[
            rec("r1", success=True, oracle_verified=True, final_state="RESOLVED"),
            rec("r2", failure="wrong_mechanism", final_state="FAILED"),
            rec("r3", failure="wrong_mechanism", final_state="FAILED"),
            rec("r4", final_state="BLOCKED"),  # no category given: counted as such, not hidden
        ],
    )

    summary = batch_summary(batch, "local")

    assert (summary.n, summary.outcomes.successes) == (4, 1)
    assert summary.outcomes.failures_by_category == {"wrong_mechanism": 2, "uncategorized": 1}
    assert summary.outcomes.by_final_state == {"RESOLVED": 1, "FAILED": 2, "BLOCKED": 1}


def test_a_pending_approval_shows_on_the_summary() -> None:
    proposal = MitigationProposal(
        proposal_id="prop-1",
        incident_id="inc-1",
        action="restart_service",
        target="cart",
        rationale="r",
    )
    pending = ApprovalRequest(
        proposal_id="prop-1",
        run_id="run-1",
        incident_id="inc-1",
        proposal_digest=proposal.digest(),
        proposal=proposal,
        requested_at=T0,
        expires_at=T0 + timedelta(minutes=15),
    )

    summary = run_summary(
        record(IncidentState.AWAITING_APPROVAL, pending_approval=pending), "running"
    )

    assert summary.pending_approval and summary.outcome == "in_progress"


def test_a_run_without_a_checkpoint_yet_gets_empty_views_not_errors() -> None:
    """The moment after POST /incidents/run: the server knows the run, nothing is saved."""
    started = [event(1, C.STATE, {"from": None, "to": "CREATED"}, stage="CREATED")]

    timeline = timeline_view("run-1", None, started, "running")
    evidence = evidence_view("run-1", None, [], started, "running")
    retrieval = retrieval_view("run-1", None, started, "running")
    usage = usage_view("run-1", None, started, "running")

    assert timeline.live and timeline.state is None and [e.seq for e in timeline.entries] == [1]
    assert evidence.findings == [] and "run record" in evidence.not_recorded[0]
    assert retrieval.searches == [] and not retrieval.memory.enabled
    assert usage.totals.model_requests == 0 and usage.budget.limits is None


def test_a_hybrid_run_says_which_retrieval_facts_it_did_not_record() -> None:
    from recap.core.models import RetrievalInfo

    info = RetrievalInfo(
        mode="hybrid",
        embedder="ollama:nomic-embed-text",
        server={"ollama_version": "0.34.4"},
        embedding_requests=2,
        replayed_requests=2,
    )

    view = retrieval_view("run-1", record(retrieval=info), [], "finished")

    assert (view.mode, view.embedder, view.embedding_requests) == (
        "hybrid",
        "ollama:nomic-embed-text",
        2,
    )
    assert view.embedding_server == {"ollama_version": "0.34.4"}
    assert "vector index identity (the stored runbook vectors searched)" in view.not_recorded


def test_a_trace_of_thousands_of_events_is_served_in_small_pages() -> None:
    """A long run stays readable: each page holds at most `limit` entries, with the cursor."""
    trace = [routing(1, 1)]
    for seq in range(2, 2502):
        category = C.TOOL_CALL if seq % 2 else C.TOOL_RESULT
        trace.append(event(seq, category, {"tool": "get_service_logs"}, agent="investigator"))
    pages = []
    after = 0
    while True:
        page = timeline_view("run-1", None, trace, "finished", after_seq=after, limit=500)
        pages.append([entry.seq for entry in page.entries])
        after = page.next_after_seq
        if not page.has_more:
            break

    assert [len(page) for page in pages] == [500, 500, 500, 500, 500, 1]
    assert [seq for page in pages for seq in page] == list(range(1, 2502))
    assert page.total_events == 2501 and page.counts["tool_call"] == 1250
