"""Evaluation records (M4): one EvalRecord per run, a failure category for every other end.

The categories are checked on hand-built run records and traces (every rule, in order), then
on real runs through the whole system: a scripted run, a replay of a committed real-model
recording, and a fake model server that reports its own processing time. Last, every
published batch under evals/reports/batches/ is checked for the promises a batch makes:
one mode, success only with the oracle's verified recovery, no local path, no secret.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals.categorize import (
    PUBLISHED_DIR,
    batch_markdown,
    categorize,
    clean_text,
    model_timing,
    outcome_counts,
    publish_batch,
    publish_problems,
    record_from_run,
    run_events,
)
from evals.records import EvalRecord, find_batches, load_batch, make_batch
from recap.core.config import REPO_ROOT, Settings
from recap.core.models import (
    ActionRecord,
    AgentFinding,
    AgentRole,
    Disagreement,
    EnvironmentInfo,
    Evidence,
    FailureMechanism,
    Hypothesis,
    HypothesisCheck,
    HypothesisStatus,
    IncidentState,
    ProviderInfo,
    RecoveryResult,
    RunConfig,
    RunRecord,
    SourceType,
    StateChange,
    TraceEvent,
    VerificationReport,
)
from recap.providers import recordings
from recap.providers.factory import REPLAY_ENV, create_provider
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from scripts import eval_replay
from tests.llm_fakes import OLLAMA_MODEL, FakeOllama, investigation_request
from tests.unit.test_published_artifacts import LOCAL_PATHS, SECRET_SHAPES, decoded

S = IncidentState
T0 = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)
RUN = "run-0000000001"
SIMULATED = EnvironmentInfo(kind="simulated", name="sim", namespace="shop", is_simulated=True)
SCRIPTED = ProviderInfo(name="deterministic-scripted-policy", kind="scripted", origin="scripted")
# A predeclared answer: the cause is resource pressure on "store" (a name no shipped scenario
# uses: the rules must work for any service name).
EXPECTED = {"root_cause_service": "store", "mechanism": "resource_pressure"}
RECORDINGS = REPO_ROOT / "fixtures" / "recordings"


# --- hand-built runs ----------------------------------------------------------------------------


def run_record(state: IncidentState, *, path: tuple[str, ...] = (), **fields: Any) -> RunRecord:
    """A finished run's record that passed `path` (then `state`), one second per state."""
    states = ["CREATED", "RETRIEVING", "INVESTIGATING", *path, state.value]
    history = [StateChange(state=S(s), at=T0 + timedelta(seconds=i)) for i, s in enumerate(states)]
    fields.setdefault("provider_info", SCRIPTED)
    return RunRecord(
        run_id=RUN,
        scenario_id="scenario-x",
        config=RunConfig(),
        state=state,
        history=history,
        environment=SIMULATED,
        **fields,
    )


def event(seq: int, category: str, payload: dict[str, Any], agent: str | None = None) -> TraceEvent:
    return TraceEvent(
        event_id=f"evt-{seq:04d}",
        run_id=RUN,
        seq=seq,
        stage="INVESTIGATING",
        ts=T0 + timedelta(seconds=seq),
        agent=agent,
        category=category,
        payload=payload,
    )


def looked_at(*services: str) -> list[TraceEvent]:
    """One tool call per service, as the gateway traces it."""
    return [
        event(
            i,
            C.TOOL_CALL,
            {"tool": "get_service_logs", "caller": "investigator", "args": {"service": s}},
        )
        for i, s in enumerate(services, start=1)
    ]


def claim(mechanism: str, target: str, **details: str) -> Hypothesis:
    return Hypothesis(
        claim="model text, never read by the rules",
        mechanism=FailureMechanism(mechanism),
        target_service=target,
        details=details,
    )


def found(
    *claims: Hypothesis, outcome: str = "hypothesis", error: str | None = None
) -> AgentFinding:
    return AgentFinding(
        agent_role=AgentRole.INVESTIGATOR,
        hypotheses=list(claims),
        started_at=T0,
        finished_at=T0,
        outcome=outcome,  # type: ignore[arg-type]
        error=error,
    )


def checked(hypothesis: Hypothesis, status: HypothesisStatus, detail: str = "") -> HypothesisCheck:
    return HypothesisCheck(
        hypothesis=hypothesis.model_copy(update={"status": status}), detail=detail
    )


def recovery(verified: bool) -> RecoveryResult:
    return RecoveryResult(
        healthy_before=False,
        healthy_after=verified,
        oracle_name="simulated-health-oracle",
        verified=verified,
        checked_at=T0,
    )


def unsolved(*claims: Hypothesis, report: VerificationReport | None = None) -> RunRecord:
    return run_record(
        S.FAILED,
        findings=[found(*claims)],
        verification=report or VerificationReport(status="insufficient"),
        outcome_note="evidence stayed insufficient after the allowed investigation rounds",
    )


# --- the categories, rule by rule ---------------------------------------------------------------


def test_success_needs_the_oracles_verified_recovery() -> None:
    done = run_record(
        S.RESOLVED, path=("PROPOSING", "MITIGATING", "VERIFYING"), recovery=recovery(True)
    )
    assert categorize(done, [], EXPECTED)[0] == ""
    record = record_from_run(done, [], mode="scripted_regression")
    assert (record.success, record.oracle_verified, record.failure) == (True, True, "")

    # A RESOLVED state the oracle did not verify is never counted as a success.
    claimed = run_record(S.RESOLVED)
    category, detail = categorize(claimed, [], EXPECTED)
    assert category == "error" and "not verified" in detail
    assert not record_from_run(claimed, [], mode="scripted_regression").success


def test_stops_are_read_from_the_runs_state() -> None:
    quota = run_record(S.QUOTA_EXHAUSTED, outcome_note="deferred: budget exhausted: provider_quota")
    assert categorize(quota, [], EXPECTED)[0] == "quota_cap"

    budget = run_record(S.BUDGET_EXHAUSTED, outcome_note="stopped safely")
    stop = event(9, C.BUDGET, {"limit": "max_tool_calls", "detail": "3 of 3 used"})
    assert categorize(budget, [stop], EXPECTED) == (
        "budget_exhausted",
        "limit max_tool_calls: 3 of 3 used",
    )

    cancelled = run_record(S.FAILED, outcome_note="run cancelled")
    assert categorize(cancelled, [], EXPECTED)[0] == "cancelled"


def test_a_blocked_run_says_who_refused() -> None:
    def blocked(result: str) -> RunRecord:
        action = ActionRecord(action="restart_service", target="store", result=result, note="n")
        return run_record(S.BLOCKED, path=("PROPOSING",), actions=[action], outcome_note="x")

    assert categorize(blocked("rejected"), [], EXPECTED)[0] == "approval_rejected"
    assert categorize(blocked("expired"), [], EXPECTED)[0] == "approval_rejected"
    assert categorize(blocked("denied"), [], EXPECTED)[0] == "safety_blocked"
    # The namespace allowlist refuses before anything runs: no action at all.
    assert categorize(run_record(S.BLOCKED, outcome_note="namespace"), [], EXPECTED)[0] == (
        "safety_blocked"
    )


def test_errors_tell_a_model_server_failure_from_a_stale_recording_and_a_crash() -> None:
    def failed(error: str) -> RunRecord:
        return run_record(S.FAILED, error=error, outcome_note="unexpected error; see run.error")

    assert categorize(failed("ProviderError: HTTP 500"), [], EXPECTED)[0] == "provider_failure"
    assert categorize(failed("FreeStatusLost: not free"), [], EXPECTED)[0] == "provider_failure"
    category, detail = categorize(failed("ReplayMismatch: no answer"), [], EXPECTED)
    assert category == "error" and detail.startswith("the replay left its recording")
    assert categorize(failed("ValueError: boom"), [], EXPECTED)[0] == "error"


def test_every_agent_failing_is_a_time_limit_or_the_model_server() -> None:
    timeouts = run_record(
        S.FAILED,
        findings=[found(outcome="error", error="timeout after 840s")] * 2,
        error="a: timeout after 840s; b: timeout after 840s",
    )
    assert categorize(timeouts, [], EXPECTED)[0] == "budget_exhausted"
    crashed = run_record(
        S.FAILED,
        findings=[found(outcome="error", error="agent crashed: ProviderError: refused")],
        error="investigator: agent crashed: ProviderError: refused",
    )
    assert categorize(crashed, [], EXPECTED)[0] == "provider_failure"
    no_answer = run_record(S.FAILED, findings=[found(outcome="no_usable_answer")])
    assert categorize(no_answer, [], EXPECTED)[0] == "provider_failure"


def test_a_fix_that_ran_without_recovery_is_the_oracles_verdict() -> None:
    action = ActionRecord(action="restart_service", target="store", result="not_recovered")
    record = run_record(
        S.FAILED,
        path=("PROPOSING", "MITIGATING", "VERIFYING", "INVESTIGATING"),
        actions=[action],
        recovery=recovery(False),
        findings=[found()],
        outcome_note="mitigation did not recover the system; attempts used up",
    )
    category, detail = categorize(record, looked_at("store"), EXPECTED)
    assert category == "oracle_not_recovered" and "restart_service on store" in detail
    # It stays the decisive fact when the next epoch's agents then run out of time.
    later = record.model_copy(
        update={
            "findings": [found(outcome="error", error="timeout after 840s")],
            "error": "investigator: timeout after 840s",
        }
    )
    category, detail = categorize(later, looked_at("store"), EXPECTED)
    assert category == "oracle_not_recovered" and detail.endswith("timeout after 840s")


def test_supported_causes_still_in_conflict_go_to_a_person() -> None:
    conflict = Disagreement(
        kind="competing_root_causes",
        claims=["bad deployment on a", "resource pressure on b"],
        resolved=False,
        question="which one?",
    )
    report = VerificationReport(status="insufficient", disagreements=[conflict])
    record = unsolved(claim("resource_pressure", "store"), report=report)
    category, detail = categorize(record, looked_at("store"), EXPECTED)
    assert category == "open_disagreement" and "bad deployment on a" in detail


def test_an_unsolved_investigation_is_judged_against_the_predeclared_answer() -> None:
    elsewhere = claim("resource_pressure", "front")
    assert categorize(unsolved(elsewhere), looked_at("front"), EXPECTED) == (
        "missed_crucial_tool",
        "no tool call named store and no recorded observation showed it; the calls named front",
    )
    assert categorize(unsolved(claim("bad_deployment", "store")), looked_at("store"), EXPECTED) == (
        "wrong_mechanism",
        "the claims on store named bad_deployment, not resource_pressure",
    )
    assert categorize(unsolved(elsewhere), looked_at("front", "store"), EXPECTED) == (
        "wrong_service",
        "the claims named front; none named store",
    )
    right = claim("resource_pressure", "store")
    report = VerificationReport(
        status="insufficient", checks=[checked(right, HypothesisStatus.INSUFFICIENT)]
    )
    category, detail = categorize(unsolved(right, report=report), looked_at("store"), EXPECTED)
    assert category == "insufficient_evidence" and "found it insufficient" in detail
    # Nothing to compare with: the scenario predeclares no answer.
    assert categorize(unsolved(elsewhere), looked_at("front"), {})[0] == "insufficient_evidence"


def test_a_namespace_wide_read_that_listed_the_root_counts_as_seeing_it() -> None:
    """A read with no service argument (all events of the namespace) that listed the root
    service's own events saw it: the run did not miss the crucial tool, it misread."""
    elsewhere = claim("resource_pressure", "front")
    listing = Evidence(
        id="ev-a1b2c3-004",
        incident_id="inc-1",
        source_type=SourceType.TOOL,
        source_ref="mcp:get_k8s_like_events(limit=20)",
        timestamp=T0,
        content="(events)",
        data={"events": [{"service": "store", "reason": "OOMKilled"}, {"service": "front"}]},
    )
    blind = Evidence(**{**listing.model_dump(), "id": "ev-a1b2c3-005", "data": {"events": []}})
    trace = looked_at("front")
    assert categorize(unsolved(elsewhere), trace, EXPECTED, evidence=[listing])[0] == (
        "wrong_service"
    )
    assert categorize(unsolved(elsewhere), trace, EXPECTED, evidence=[blind])[0] == (
        "missed_crucial_tool"
    )


def test_a_dependency_link_to_the_root_counts_unless_the_verifier_contradicted_it() -> None:
    link = claim("dependency_failure", "front", dependency="store")
    traced = categorize(unsolved(link), looked_at("front", "store"), EXPECTED)
    assert traced[0] == "insufficient_evidence" and "traced the failure to store" in traced[1]

    report = VerificationReport(
        status="insufficient", checks=[checked(link, HypothesisStatus.CONTRADICTED, "store")]
    )
    wrong = categorize(unsolved(link, report=report), looked_at("front", "store"), EXPECTED)
    assert wrong[0] == "wrong_service"


@pytest.mark.parametrize(
    "names",
    [("store", "front"), ("inventory", "frontend"), ("payment", "checkout"), ("x9", "y9")],
)
def test_the_rules_hold_whatever_the_services_are_called(names: tuple[str, str]) -> None:
    """Generic rules: renaming the services changes no category (no name is in the rules)."""
    root, other = names
    expected = {"root_cause_service": root, "mechanism": "resource_pressure"}
    cases = [
        (unsolved(claim("resource_pressure", other)), looked_at(other)),
        (unsolved(claim("bad_deployment", root)), looked_at(root)),
        (unsolved(claim("resource_pressure", other)), looked_at(other, root)),
        (unsolved(claim("dependency_failure", other, dependency=root)), looked_at(root)),
    ]
    assert [categorize(r, e, expected)[0] for r, e in cases] == [
        "missed_crucial_tool",
        "wrong_mechanism",
        "wrong_service",
        "insufficient_evidence",
    ]


# --- records and batches keep the modes apart ------------------------------------------------


def test_a_record_refuses_a_run_from_another_mode() -> None:
    record = run_record(S.FAILED)
    for mode in ("real_llm", "offline_replay"):
        with pytest.raises(ValueError, match="scripted"):
            record_from_run(record, [], mode=mode)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="M5"):
        record_from_run(record, [], mode="kubernetes_future")
    live = record.model_copy(
        update={
            "provider_info": ProviderInfo(name="ollama:qwen3:8b", kind="llm", origin="local_llm")
        }
    )
    assert record_from_run(live, [], mode="real_llm").provider_class == "local_llm"
    with pytest.raises(ValueError):
        record_from_run(live, [], mode="scripted_regression")
    cluster = record.model_copy(
        update={"environment": SIMULATED.model_copy(update={"kind": "kind", "is_simulated": False})}
    )
    with pytest.raises(ValueError, match="not the simulated environment"):
        record_from_run(cluster, [], mode="scripted_regression")


def test_a_batch_holds_one_mode_and_never_the_future_cluster_mode() -> None:
    scripted = record_from_run(run_record(S.FAILED), [], mode="scripted_regression")
    replayed = scripted.model_copy(update={"mode": "offline_replay"})
    common: dict[str, Any] = {"batch_id": "b", "title": "t", "created_at": T0}
    with pytest.raises(ValueError, match="another mode"):
        make_batch(mode="scripted_regression", records=[scripted, replayed], **common)
    with pytest.raises(ValueError, match="M5"):
        make_batch(mode="kubernetes_future", records=[], **common)
    batch = make_batch(mode="scripted_regression", records=[scripted], **common)
    assert (batch.n, batch.providers, batch.models) == (1, ["deterministic-scripted-policy"], [])


def test_stage_times_keep_the_approval_wait_apart() -> None:
    record = run_record(
        S.RESOLVED,
        path=("PROPOSING", "AWAITING_APPROVAL", "MITIGATING", "VERIFYING"),
        recovery=recovery(True),
    )
    # The approver took 120 s: AWAITING_APPROVAL is its own stage, not part of the work.
    history = list(record.history)
    later = [c.model_copy(update={"at": c.at + timedelta(seconds=120)}) for c in history[5:]]
    record = record.model_copy(update={"history": [*history[:5], *later]})
    evaluated = record_from_run(record, [], mode="scripted_regression")
    stages = {s.stage: s.seconds for s in evaluated.stages}
    assert stages["AWAITING_APPROVAL"] == 121.0 and stages["INVESTIGATING"] == 1.0
    assert "RESOLVED" not in stages  # the end has no duration
    assert evaluated.seconds == sum(stages.values()) == 127.0
    assert evaluated.ended_at == history[-1].at + timedelta(seconds=120)


def test_model_time_is_split_into_server_time_and_waiting() -> None:
    """The wait inside the model server is its own time minus its compute: Ollama's
    total_duration counts from when it received the request, so a queue is inside it.
    (Request time minus server time is transport, never queueing.)"""

    def llm(
        seq: int,
        duration: float,
        server: float | None,
        compute: float | None = None,
        replayed: bool = False,
    ) -> TraceEvent:
        payload = {
            "duration_ms": duration,
            "server_ms": server,
            "server_compute_ms": compute,
            "replayed": replayed,
        }
        return event(seq, C.LLM_CALL, payload, agent="investigator")

    calls = [llm(1, 30_000, 29_800, 9_800), llm(2, 40_000, 39_700, 39_700), llm(3, 5_000, None)]
    timing = model_timing([*calls, llm(4, 99_000, 1_000, 500, replayed=True)])
    assert (timing.requests, timing.seconds) == (3, 75.0)  # a replayed answer is no model time
    assert (timing.server_requests, timing.server_seconds, timing.waiting_seconds) == (
        2,
        69.5,
        20.0,  # the first request waited 20 s inside the server; the second computed throughout
    )
    # A server that reports its time but not its compute: the wait is not known, never guessed.
    unsplit = model_timing([llm(1, 30_000, 29_800)])
    assert (unsplit.server_seconds, unsplit.waiting_seconds) == (29.8, None)
    assert model_timing([]).seconds is None  # the scripted policy sends no request


def test_a_long_trace_is_read_page_by_page() -> None:
    class Source:
        def __init__(self, events: list[TraceEvent]) -> None:
            self.events = events

        def list_events(
            self, run_id: str, after_seq: int = 0, limit: int = 5000
        ) -> list[TraceEvent]:
            return [e for e in self.events if e.seq > after_seq][:limit]

    events = [event(i, C.TOOL_CALL, {}) for i in range(1, 13)]
    assert [e.seq for e in run_events(Source(events), RUN, page=5)] == list(range(1, 13))


def test_published_text_never_carries_a_local_folder_or_a_secret(tmp_path: Path) -> None:
    home = str(Path.home())
    assert clean_text(f"recording not found: {home}/x/20261025-ollama.jsonl") == (
        "recording not found: 20261025-ollama.jsonl"
    )
    assert clean_text("could not reach http://127.0.0.1:11434/api/chat") == (
        "could not reach http://127.0.0.1:11434/api/chat"
    )
    assert "gsk_" not in clean_text("key gsk_" + "A" * 30)

    record = record_from_run(run_record(S.FAILED), [], mode="scripted_regression")
    batch = make_batch(
        batch_id="leaky", mode="scripted_regression", title="t", records=[record], created_at=T0
    )
    assert publish_problems(batch) == []
    leaky = batch.model_copy(update={"methods": [f"made in {home}/somewhere"]})
    assert publish_problems(leaky) == ["it names a local folder"]
    with pytest.raises(ValueError, match="refused"):
        publish_batch(leaky, tmp_path)
    assert list(tmp_path.iterdir()) == []  # nothing was written
    path = publish_batch(batch, tmp_path)
    assert (path.parent / "README.md").read_text(encoding="utf-8") == batch_markdown(batch)


# --- real runs ----------------------------------------------------------------------------------


@pytest.mark.e2e
async def test_a_scripted_run_becomes_a_complete_record(settings: Settings) -> None:
    runtime = Runtime(settings)
    record = await runtime.run_incident(
        "bad-deployment", RunConfig(approval_mode="auto", use_memory=False)
    )
    events = runtime.events.list_events(record.run_id)
    scenario = next(s for s in runtime.list_scenarios() if s.id == "bad-deployment")
    evaluated = record_from_run(record, events, mode="scripted_regression", scenario=scenario)

    assert (evaluated.success, evaluated.failure, evaluated.final_state) == (True, "", "RESOLVED")
    assert (evaluated.incident_family, evaluated.split) == ("bad_deployment", "demo")
    assert evaluated.expected == scenario.expected  # the predeclared answer, kept for readers
    assert (evaluated.diagnosis, evaluated.diagnosis_correct) == ("bad_deployment@checkout", True)
    assert (evaluated.provider_class, evaluated.model, evaluated.prompt_version) == (
        "scripted",
        None,
        None,  # the scripted policy uses no prompts
    )
    assert evaluated.config_hash == record.versions["config_hash"]
    assert evaluated.code_version == record.versions["code_version"]
    # Tool calls per caller: the investigator's reads, and the controller's one write.
    assert evaluated.tool_calls_by_agent["controller"] == 1
    assert evaluated.tool_calls == sum(evaluated.tool_calls_by_agent.values())
    assert evaluated.tool_calls == record.usage.tool_calls  # the budget counted the same calls
    assert evaluated.topology == [f"{s.mode.value}: investigator" for s in record.strategies]
    assert evaluated.gateway_outcome == (
        "execute_rollback on checkout: APPROVAL_REQUIRED (R11-reversible-approval); "
        "evidence check passed; executed"
    )
    assert evaluated.approvals == ["approved by auto-approver[simulation]"]
    assert evaluated.proposed_actions[0].startswith("execute_rollback on checkout (to_version=")
    assert "bad_deployment@checkout" in evaluated.supported_hypotheses
    assert evaluated.retrieval_mode == "bm25" and evaluated.retrieved_docs
    # No model ran: nothing reported, no model time; RECAP's own estimate is kept apart.
    assert (evaluated.model_requests, evaluated.input_tokens, evaluated.model_seconds) == (
        0,
        None,
        None,
    )
    assert evaluated.estimated_tokens == record.usage.estimated_tokens > 0
    stages = [s.stage for s in evaluated.stages]
    assert stages == [
        "CREATED",
        "RETRIEVING",
        "INVESTIGATING",
        "PROPOSING",
        "AWAITING_APPROVAL",
        "MITIGATING",
        "VERIFYING",
    ]
    assert evaluated.seconds == pytest.approx(sum(s.seconds for s in evaluated.stages), abs=0.01)
    assert evaluated.recording is None
    assert EvalRecord.model_validate_json(evaluated.model_dump_json()) == evaluated


async def test_ollama_reports_its_own_processing_time(settings: Settings) -> None:
    """total_duration (nanoseconds) becomes ModelCall.server_ms; without it, None."""

    def answer(**extra: Any) -> httpx.Response:
        call = {"function": {"name": "get_service_health", "arguments": {"service": "checkout"}}}
        body = {
            "model": OLLAMA_MODEL,
            "message": {"role": "assistant", "content": "", "tool_calls": [call]},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 1200,
            "eval_count": 40,
            **extra,
        }
        return httpx.Response(200, json=body)

    timed = answer(
        total_duration=2_345_600_000,
        load_duration=100_000_000,
        prompt_eval_duration=900_000_000,
        eval_duration=300_000_000,
    )
    fake = FakeOllama([timed, answer(total_duration=2_000_000_000), answer()])
    provider = create_provider("ollama", settings, mock_http=fake.mock())
    first = await provider.next_action(investigation_request(settings))
    second = await provider.next_action(investigation_request(settings))
    third = await provider.next_action(investigation_request(settings))
    assert [(c.server_ms, c.server_compute_ms) for c in first.model_calls] == [(2345.6, 1300.0)]
    # Compute needs all three parts; a missing part is never assumed to be zero.
    assert [(c.server_ms, c.server_compute_ms) for c in second.model_calls] == [(2000.0, None)]
    assert [(c.server_ms, c.server_compute_ms) for c in third.model_calls] == [(None, None)]


@pytest.mark.e2e
async def test_a_replay_counts_no_model_time_and_keeps_the_recorded_server_time(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REPLAY_ENV, str(RECORDINGS / "ollama-qwen3-8b-bad-deployment.jsonl"))
    runtime = Runtime(settings)
    record = await runtime.run_incident(
        "bad-deployment", RunConfig(provider="replay", use_memory=False, approval_mode="auto")
    )
    events = runtime.events.list_events(record.run_id)
    calls = [e.payload for e in events if e.category == C.LLM_CALL]
    # The recorded answers carry Ollama's total_duration from the original run ...
    assert calls and all(c["replayed"] and c["server_ms"] > 0 for c in calls)
    evaluated = record_from_run(record, events, mode="offline_replay")
    # ... but a replay sent no request: no requests, no tokens, no model time of its own.
    assert (evaluated.model_requests, evaluated.input_tokens, evaluated.model_seconds) == (
        0,
        None,
        None,
    )
    assert evaluated.success and evaluated.provider_class == "replay"
    assert evaluated.recording == "ollama-qwen3-8b-bad-deployment.jsonl"  # a name, never a path
    with pytest.raises(ValueError):
        record_from_run(record, events, mode="real_llm")  # a replay is never a live run


# --- the published batches ------------------------------------------------------------------


def published() -> list[Path]:
    return sorted(PUBLISHED_DIR.glob("*/batch.json"))


def test_the_published_batches_cover_each_implemented_mode() -> None:
    batches = find_batches([PUBLISHED_DIR])
    assert len(batches) == len(published())  # every file loads
    modes = {b.mode for b in batches}
    assert {"scripted_regression", "offline_replay", "real_llm"} <= modes
    assert "kubernetes_future" not in modes  # M5: no batch may claim a real cluster


@pytest.mark.parametrize("path", published(), ids=lambda p: p.parent.name)
def test_a_published_batch_keeps_its_promises(path: Path) -> None:
    batch = load_batch(path)
    assert batch.batch_id == path.parent.name
    assert batch.records and batch.methods and batch.limitations
    assert any("SIMULATED" in line or "simulated" in line for line in batch.limitations)
    for record in batch.records:
        assert record.mode == batch.mode
        assert record.success == (record.final_state == "RESOLVED" and record.oracle_verified), (
            record.run_id
        )
        assert (record.failure == "") == record.success, record.run_id
        assert record.recording is None or "/" not in record.recording  # a name, not a path
    # The readable digest is the batch's own, and its counts are the records'.
    readme = (path.parent / "README.md").read_text(encoding="utf-8")
    assert readme == batch_markdown(batch)
    assert f"N = {batch.n}" in readme
    for outcome, count in outcome_counts(batch.records).items():
        assert f"| {outcome} | {count} of {batch.n} |" in readme
    # Nothing local, nothing secret (the published-artifact checks, decoded).
    for file in (path, path.parent / "README.md"):
        values, keys = decoded(file)
        text = "\n".join(values).lower()
        assert not any(marker in text for marker in LOCAL_PATHS), file.name
        assert not any(p.search(v) for v in values for p in SECRET_SHAPES.values()), file.name
    assert publish_problems(batch) == []


def test_published_batches_round_trip_unchanged() -> None:
    for path in published():
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert json.loads(load_batch(path).model_dump_json()) == raw


def test_a_shuffled_trace_gives_the_same_categories() -> None:
    """Categories never depend on the order trace events arrive in."""
    record = unsolved(claim("bad_deployment", "store"))
    events = looked_at("front", "store", "other")
    shuffled = list(events)
    random.Random(7).shuffle(shuffled)
    assert categorize(record, shuffled, EXPECTED) == categorize(record, events, EXPECTED)


# --- the offline replay batch (scripts/eval_replay.py) ---------------------------------------


@pytest.fixture
def replay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No recording chosen by the shell, and no simulated latency (a fast replay)."""
    monkeypatch.delenv(REPLAY_ENV, raising=False)
    monkeypatch.setenv("RECAP_TOOL_LATENCY_MS", "0")


@pytest.mark.e2e
@pytest.mark.usefixtures("replay_env")
async def test_every_committed_recording_replays_into_one_offline_replay_batch(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out"
    assert await eval_replay.main(["--out", str(out)]) == 0

    batch = load_batch(out / "batch.json")
    fixtures = recordings.list_fixtures()
    assert (batch.mode, batch.plan) == ("offline_replay", "fixtures/recordings/MANIFEST.json")
    assert [r.recording for r in batch.records] == [f.file for f in fixtures]
    assert [r.scenario_id for r in batch.records] == [f.scenario for f in fixtures]
    for record in batch.records:
        assert (record.success, record.provider_class) == (True, "replay")
        assert (record.model_requests, record.input_tokens, record.model_seconds) == (0, None, None)
        assert record.approvals == ["approved by auto-approver[simulation]"]
    # Two specialists in two rounds for the golden recording, as its manifest entry says.
    golden = batch.records[[f.id for f in fixtures].index("qwen3-8b-unfamiliar-parallel")]
    assert golden.rounds == 2 and golden.topology[0].startswith("parallel")
    local = out / "data" / "evals" / "batches" / batch.batch_id / "batch.json"
    assert load_batch(local) == batch
    assert (out / "batch.md").read_text(encoding="utf-8") == batch_markdown(batch)


@pytest.mark.usefixtures("replay_env")
async def test_the_replay_script_refuses_what_could_replay_the_wrong_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = ["--out", str(tmp_path / "out")]
    monkeypatch.setenv(REPLAY_ENV, str(RECORDINGS / "ollama-qwen3-8b-bad-deployment.jsonl"))
    assert await eval_replay.main(out) == eval_replay.EXIT_REFUSED  # the shell would choose
    monkeypatch.delenv(REPLAY_ENV)
    assert await eval_replay.main([*out, "--only", "no-such-recording"]) == 2
    assert await eval_replay.main([*out, "--data-dir", str(REPO_ROOT / "data")]) == 2
    said = capsys.readouterr().err
    assert REPLAY_ENV in said and "no-such-recording" in said and "data/" in said
    assert not (tmp_path / "out").exists()  # refused before anything was written


@pytest.mark.usefixtures("replay_env")
async def test_an_altered_recording_is_not_replayed_and_the_batch_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content addressing: one changed byte and the recording is refused, never replayed."""
    folder = tmp_path / "recordings"
    folder.mkdir()
    [entry] = [
        f.model_dump() for f in recordings.list_fixtures() if f.id == "qwen3-8b-bad-deployment"
    ]
    text = (RECORDINGS / entry["file"]).read_text(encoding="utf-8")
    (folder / entry["file"]).write_text(text.replace("checkout", "checkouT", 1), encoding="utf-8")
    manifest = folder / "MANIFEST.json"
    manifest.write_text(json.dumps({"schema": recordings.SCHEMA, "fixtures": [entry]}))
    monkeypatch.setattr(recordings, "MANIFEST", manifest)

    out = tmp_path / "out"
    assert await eval_replay.main(["--out", str(out)]) == eval_replay.EXIT_FAILED
    batch = load_batch(out / "batch.json")
    assert batch.records == []
    assert any(
        line.startswith("Not replayed: qwen3-8b-bad-deployment") and "altered" in line
        for line in batch.limitations
    )
