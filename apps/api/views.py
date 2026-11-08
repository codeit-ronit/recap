"""Build the API's typed views (apps/api/dto.py) from what runs persisted.

    RunRecord (the checkpoint)  --+
    Evidence  (the registry)    --+--> run_summary, timeline_view, evidence_view,
    TraceEvent (the stored trace) +    retrieval_view, usage_view  --> JSON responses

Every function here is pure: it reads the records it is given and returns a model, with
no database, file or network access (apps/api/service.py does the reading). So each view
can be tested with hand-made records, including old, partial and broken ones.

What a view may do with a record: copy a field, count or add up recorded values, and
match recorded ids with each other. What it may not do: fill a gap with a guess. A field
the record lacks stays null and is named in `not_recorded`.

Text written by a model, a tool, an alert or a runbook is passed on as data and labeled
with who wrote it; RECAP's own text (rule lines, the router's reasons) is labeled RECAP's.
`LocalPaths.scrub` removes local folder names from everything a response carries.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

from pydantic import ValidationError

from apps.api.dto import (
    AdapterStatus,
    AgentLane,
    BatchOutcomes,
    BatchSource,
    BatchSummary,
    BatchView,
    BestClaim,
    BudgetView,
    CheckView,
    ClaimView,
    ComponentStatus,
    EmbeddingCallView,
    EmbeddingUsageView,
    EstimatedTokens,
    EvalModeInfo,
    EvalsView,
    EvidenceItem,
    EvidenceView,
    ExecutionBackend,
    ExportProvenance,
    FeatureFlags,
    FindingView,
    FixtureIntegrity,
    FixtureView,
    InjectionFlagView,
    JobStatus,
    JobView,
    MemoryAccessView,
    MemoryAssessmentView,
    MemoryHitView,
    MemoryRetrievalView,
    MemorySearchView,
    MemoryWriteView,
    ModelRequest,
    ModelUsageView,
    OracleVerdict,
    PolicyVerdictView,
    ProposalCheckView,
    ProposalView,
    ProviderLimitsView,
    ProviderView,
    PublishedProvenance,
    PublishedView,
    QueueView,
    RecordWithoutTraceView,
    RefusedSearchView,
    RejectedArtifactView,
    ReplayProvenance,
    ReplaysView,
    RetrievalView,
    RetryCounts,
    RoundInfo,
    RunOutcome,
    RunStatus,
    RunSummary,
    SearchResultView,
    SearchView,
    SkipView,
    SystemView,
    TextAuthor,
    TimelineEntry,
    TimelineFilters,
    TimelineKind,
    TimelineView,
    TrustClass,
    UsageTotals,
    UsageView,
    VerificationView,
    VersionsView,
    WorkerView,
)
from evals.published import PublishedIndex, PublishedRun
from evals.records import EvalBatch
from recap.core.config import ProviderSettings
from recap.core.models import (
    TERMINAL_STATES,
    Disagreement,
    EnvironmentInfo,
    Evidence,
    IncidentState,
    MemoryHit,
    RetrievedChunk,
    RunRecord,
    SourceType,
    TraceEvent,
    TrustLevel,
)
from recap.providers.recordings import FixtureInfo
from recap.telemetry.tracer import TraceCategory as C
from recap.workers.queue import Job, QueueConfig
from recap.workers.resp import RedisAddress

# The controller's note for a cancelled run (IncidentController.run): the record keeps no
# other sign of a cancellation, so the outcome reads it from there.
CANCELLED_NOTE = "run cancelled"

SIMULATION_NOTICE = (
    "SIMULATED ENVIRONMENT: every incident runs against RECAP's local simulation of a "
    "five-service shop. No real cluster, Kubernetes or production system is touched."
)
CONCURRENCY_NOTE = (
    "A local Ollama server may answer one request at a time. When two agents investigate "
    "in parallel their turns overlap, but one agent's model request can wait inside the "
    "server for the other's. That wait is part of both duration_ms and server_ms "
    "(Ollama's total_duration counts from when it received the request). The server also "
    "reports its compute (model load, prompt evaluation, generation): server_wait_ms is "
    "server_ms minus that compute, the time a request waited inside the server."
)
REPLAY_NOTE = (
    "Model requests were answered from a recording: their token counts, durations and "
    "server times are the original run's, and no model was called in this run. Agent "
    "turn times (lanes, round wall time and overlap) are this replay's own."
)
SCRIPTED_NOTE = (
    "The scripted policy is not a model: it sends no model requests, and its token counts "
    "are RECAP's estimates."
)

# What each trust class means (EvidenceView.trust_classes).
TRUST_CLASSES: dict[str, str] = {
    "untrusted_tool_output": "an MCP tool's answer: data that might lie or carry instructions",
    "untrusted_runbook_text": "a runbook section, or the receipt of an agent's runbook search",
    "untrusted_memory_text": "what a past episode recorded",
    "untrusted_alert_text": "the alert's own words",
    "recap_trusted_text": "produced by RECAP's own code (for example the verifier's rule lines)",
}
_TRUST_BY_SOURCE: dict[SourceType, TrustClass] = {
    SourceType.TOOL: "untrusted_tool_output",
    SourceType.DOC: "untrusted_runbook_text",
    SourceType.MEMORY: "untrusted_memory_text",
    SourceType.ALERT: "untrusted_alert_text",
    SourceType.ORACLE: "recap_trusted_text",
}

# What one honest sentence says about each kind of provider (GET /providers).
PROVIDER_LIMITATIONS: dict[str, list[str]] = {
    "scripted": [
        "Not an LLM: fixed playbooks and heuristics written for the simulated incidents.",
        "Its token counts are RECAP's estimates; it calls no model and costs $0.",
    ],
    "local_llm": [
        "Runs on this machine through Ollama: no network, no charge.",
        "A single Ollama server may answer one request at a time, so parallel agents' "
        "requests can wait for each other inside it.",
        "Each run is one sample: a single success or failure is not a success rate.",
    ],
    "free_hosted_llm": [
        "A hosted free plan: RECAP's own caps sit below the plan's limits, and a quota stop "
        "defers the run (QUOTA_EXHAUSTED) instead of switching provider.",
        "Every request needs the owner's opt-in and a dated free-plan confirmation; every "
        "response's rate-limit header is checked against the free plan.",
    ],
    "replay": [
        "Replays the recorded answers of an earlier real-model run: no model is called now.",
        "A request the recording does not hold stops the run (ReplayMismatch); nothing is guessed.",
    ],
    "paid_llm": [
        "A paid API billed per call: disabled for this $0 project and never selectable "
        "from the API.",
    ],
}

EVAL_MODES: list[EvalModeInfo] = [
    EvalModeInfo(
        mode="scripted_regression",
        status="implemented",
        description="The scripted policy on the benchmark scenarios: a regression test of "
        "RECAP's own code, not evidence about any LLM.",
    ),
    EvalModeInfo(
        mode="offline_replay",
        status="implemented",
        description="A recorded real-model run replayed offline: it shows the recorded "
        "decisions still pass RECAP's path, not a new run.",
    ),
    EvalModeInfo(
        mode="real_llm",
        status="implemented",
        description="A real model (local, or a verified free plan) on simulated incidents.",
    ),
    EvalModeInfo(
        mode="real_kubernetes",
        status="implemented",
        description="A real model on a real Kubernetes cluster (the Kind demo cluster or "
        "SREGym's own, told apart by each batch's target; M5): real faults, a person's "
        "approval for every write, and the cluster's own health as RECAP's oracle. A "
        "benchmark grader's verdicts, when recorded, are kept apart.",
    ),
]

_KIND: dict[str, TimelineKind] = {
    C.STATE: "lifecycle",
    C.TOOL_DISCOVERY: "tool_discovery",
    C.RETRIEVAL: "retrieval",
    C.RETRIEVAL_SKIP: "retrieval",
    C.MEMORY_SEARCH: "memory",
    C.MEMORY_ASSESS: "memory",
    C.MEMORY_WRITE: "memory",
    C.MEMORY_SKIP: "memory",
    C.ROUTING: "routing",
    C.INVESTIGATION_ROUND: "round_start",
    C.AGENT_START: "agent_start",
    C.CONTEXT: "context",
    C.LLM_CALL: "model_request",
    C.AGENT_DECISION: "agent_decision",
    C.POLICY: "policy",
    C.TOOL_CALL: "tool_call",
    C.TOOL_RESULT: "tool_result",
    C.INJECTION: "injection_flag",
    C.AGENT_FINISH: "agent_finish",
    C.ORCHESTRATION: "round_summary",
    C.VERIFY_MERGE: "verification",
    C.VERIFY_EVIDENCE: "verification",
    C.VERIFY_RECOVERY: "oracle",
    C.PROPOSAL: "proposal",
    C.APPROVAL_REQUESTED: "approval_requested",
    C.APPROVAL_DECIDED: "approval_decided",
    C.MITIGATION_STARTED: "write_started",
    C.MITIGATION: "write_executed",
    C.BUDGET: "budget",
    C.RUN_ERROR: "error",
    C.RUN_SUMMARY: "summary",
}

# Events whose payload is designed to hold text a model, a tool or an alert wrote.
_UNTRUSTED_TEXT = frozenset(
    {
        C.AGENT_DECISION,
        C.AGENT_FINISH,
        C.LLM_CALL,
        C.VERIFY_EVIDENCE,
        C.PROPOSAL,
        C.INJECTION,
        C.RETRIEVAL,
        C.TOOL_CALL,
        C.TOOL_RESULT,
        C.RUN_ERROR,
    }
)
# Events that carry their own round number in the payload.
_ROUND_IN_PAYLOAD = frozenset({C.AGENT_START, C.AGENT_FINISH, C.INVESTIGATION_ROUND})


# ======================================================================================
# Local paths: never in a response
# ======================================================================================

# Paths of other users and of macOS temporary folders, in case one slipped through the
# known folders below (e.g. a recording named by an absolute environment variable).
_USER_HOME = re.compile(r"(?:/Users|/home)/[^/\s'\"\\]+")
_TEMP_DIR = re.compile(r"(?:/private)?/var/folders/[^/\s'\"\\]+/[^/\s'\"\\]+/T")


@dataclass(frozen=True)
class LocalPaths:
    """The absolute folders a response must never show, and what each becomes.

    The data folder becomes "data", the repository root disappears (a path inside it is
    shown repo-relative), the home folder becomes "~", like the export script does.
    """

    replacements: tuple[tuple[str, str], ...]

    @classmethod
    def of(cls, root: Path, data_dir: Path, home: Path | None = None) -> LocalPaths:
        home = home or Path.home()
        pairs: dict[str, str] = {}
        for folder, shown in ((data_dir, "data"), (root, ""), (home, "~")):
            for variant in {str(folder), str(folder.resolve())}:
                if len(variant) <= 1:  # never replace "/" itself
                    continue
                # "<root>/x" becomes "x"; "<data>/x" becomes "data/x".
                pairs[variant + os.sep] = shown + os.sep if shown else ""
                pairs[variant] = shown or "."
        # Longest first: the data folder inside the repository becomes "data", not "./data".
        ordered = sorted(pairs.items(), key=lambda pair: -len(pair[0]))
        return cls(tuple(ordered))

    def scrub(self, value: Any) -> Any:
        """A copy of a JSON-like value with every local folder replaced (see the class doc)."""
        if isinstance(value, str):
            return self._text(value)
        if isinstance(value, Mapping):
            return {key: self.scrub(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.scrub(item) for item in value]
        return value

    def _text(self, text: str) -> str:
        if "/" not in text and "\\" not in text:
            return text  # nothing that could be a path (most strings)
        for local, shown in self.replacements:
            text = text.replace(local, shown)
        text = _TEMP_DIR.sub("<tmp>", text)
        return _USER_HOME.sub("~", text)


def file_id(path: str | None) -> str | None:
    """A recording's id: its file name without folders or extension (never a local path)."""
    if not path:
        return None
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name[: -len(".jsonl")] if name.endswith(".jsonl") else name


# ======================================================================================
# Runs
# ======================================================================================


def environment_label(info: EnvironmentInfo) -> str:
    """The dashboard banner: it says SIMULATED only when the environment really is."""
    if info.is_simulated:
        return "SIMULATED LOCAL ENVIRONMENT · V1"
    return f"LIVE ENVIRONMENT ({info.kind}) · V1"


def run_outcome(record: RunRecord) -> RunOutcome:
    """How a run ended, in one word, from its state, its last action and its note."""
    state = record.state
    if state not in TERMINAL_STATES:
        return "in_progress"
    if state == IncidentState.RESOLVED:
        return "resolved"
    if state == IncidentState.BUDGET_EXHAUSTED:
        return "budget_exhausted"
    if state == IncidentState.QUOTA_EXHAUSTED:
        return "quota_deferred"
    last = record.actions[-1].result if record.actions else None
    if state == IncidentState.BLOCKED:
        if last == "rejected":
            return "approval_rejected"
        if last == "expired":
            return "approval_expired"
        return "safety_blocked"  # the policy, the evidence gate or the namespace allowlist
    if record.interruption is not None:  # closed after its worker stopped (M6)
        return "needs_reconciliation" if record.interruption.needs_reconciliation else "interrupted"
    if record.outcome_note == CANCELLED_NOTE:
        return "cancelled"
    return "error" if record.error else "failed"


def text_author(record: RunRecord | None) -> TextAuthor:
    """Who wrote the free text in a run's findings and proposals: its provider decides."""
    origin = record.provider_info.origin if record and record.provider_info else None
    if origin == "scripted":
        return "scripted_policy"
    if origin == "replay":
        return "replayed_model"
    if origin in ("local_llm", "free_hosted_llm", "paid_llm"):
        return "model"
    return "unknown"


def origin_labels(origin: str | None, *, committed_fixture: bool = False) -> list[str]:
    """What a screen must say about where a run's decisions come from.

    A replay is "RECORDED REAL-LLM RUN" only when its recording is a committed fixture: the
    manifest lists only recordings of real model runs, while any other recording (a test's
    fake server, say) is just a replay.
    """
    if origin == "replay":
        return ["REPLAY", "RECORDED REAL-LLM RUN"] if committed_fixture else ["REPLAY"]
    return {
        "scripted": ["SCRIPTED BASELINE (NOT AN LLM)"],
        "local_llm": ["LIVE LOCAL MODEL"],
        "free_hosted_llm": ["LIVE FREE HOSTED MODEL"],
        "paid_llm": ["PAID MODEL"],
    }.get(origin or "", [])


def environment_run_label(is_simulated: bool, kind: str) -> str:
    """What a run's target was: SIMULATED ENVIRONMENT, or LIVE ENVIRONMENT (<kind>)."""
    return "SIMULATED ENVIRONMENT" if is_simulated else f"LIVE ENVIRONMENT ({kind})"


def run_labels(record: RunRecord, replay: ReplayProvenance | None) -> list[str]:
    """Labels a screen must show with this run. Each comes from a recorded fact."""
    labels: list[str] = []
    if record.environment is not None:
        labels.append(
            environment_run_label(record.environment.is_simulated, record.environment.kind)
        )
    origin = record.provider_info.origin if record.provider_info else None
    committed = replay is not None and replay.committed_fixture
    return labels + origin_labels(origin, committed_fixture=committed)


def replay_provenance(
    record: RunRecord, fixtures: Sequence[FixtureInfo]
) -> ReplayProvenance | None:
    """Where a replayed run's answers came from (None for any other run)."""
    info = record.provider_info
    if info is None or info.origin != "replay":
        return None
    header = info.replay_of or {}
    recording_id = _str(header.get("recording_id"))
    digest = _str(header.get("sha256"))
    chosen = record.config.replay_fixture
    if "fixture" in header:
        # Since M4 the factory names the committed fixture whose pinned SHA-256 the replayed
        # bytes have (None: they are no committed recording).
        replayed_id = _str(header.get("fixture"))
        replayed = next((f for f in fixtures if f.id == replayed_id), None)
        committed = replayed_id is not None
        matches = None if chosen is None else replayed_id == chosen
    else:  # an older record names its recording only by id
        replayed = next((f for f in fixtures if f.recording_id == recording_id), None)
        committed = replayed is not None
        chosen_info = next((f for f in fixtures if f.id == chosen), None) if chosen else None
        matches = None if chosen_info is None else chosen_info.recording_id == recording_id
    fixture = next((f for f in fixtures if f.id == chosen), None) if chosen else None
    fixture = fixture or replayed
    pinned = fixture.sha256[:12] if fixture and committed else None
    return ReplayProvenance(
        fixture_id=chosen,
        recording_id=recording_id,
        recording_matches_fixture=matches,
        committed_fixture=committed,
        original_provider=_str(header.get("original_provider")),
        model=_str(header.get("model")),
        prompt_version=_str(header.get("prompt_version")),
        recorded_at=_str(header.get("created_at")),
        fixture_title=fixture.title if fixture else None,
        fixture_sha256=digest[:12] if digest else pinned,
        original_export=fixture.exported_run if fixture else None,
        originally_approved_by=fixture.approved_by or None if fixture else None,
    )


PUBLISHED_LABEL = "PUBLISHED · HISTORICAL · READ-ONLY"


def run_summary(
    record: RunRecord,
    status: RunStatus,
    *,
    fixtures: Sequence[FixtureInfo] = (),
    exports: Mapping[str, ExportProvenance] | None = None,
    published: PublishedProvenance | None = None,
) -> RunSummary:
    """One run's identity, provenance and outcome, from its checkpoint alone (and, for a
    published run, where its checked files are)."""
    missing: list[str] = []
    info = record.provider_info
    if info is None:
        missing.append("provider_info")
    versions = record.versions
    for key in ("config_hash", "code_version", "recap"):
        if not versions.get(key):
            missing.append(f"versions.{key}")
    environment = record.environment
    if environment is None:
        missing.append("environment")
    if not record.history:
        missing.append("history")

    started = record.history[0].at if record.history else record.created_at
    ended = None
    if record.state in TERMINAL_STATES:
        terminal = [change.at for change in record.history if change.state == record.state]
        ended = terminal[-1] if terminal else None
        if ended is None:
            missing.append("ended_at")
    recovery = record.recovery
    oracle = (
        OracleVerdict(
            oracle=recovery.oracle_name,
            verified=recovery.verified,
            healthy_before=recovery.healthy_before,
            healthy_after=recovery.healthy_after,
            checked_at=recovery.checked_at,
            services_checked=list(recovery.services_checked),
        )
        if recovery is not None
        else None
    )
    replay = replay_provenance(record, fixtures)
    own_recording = info.recording if info is not None and info.origin != "replay" else None
    return RunSummary(
        run_id=record.run_id,
        scenario_id=record.scenario_id,
        state=record.state,
        label=record.config.label,
        created_at=record.created_at,
        updated_at=record.updated_at,
        modes=[s.mode for s in record.strategies],
        recovered=bool(recovery and recovery.verified),
        outcome_note=record.outcome_note,
        provider=record.provider,
        tool_calls=record.usage.tool_calls,
        pending_approval=record.pending_approval is not None,
        status=status,
        terminal=record.state in TERMINAL_STATES,
        outcome=run_outcome(record),
        provider_class=info.origin if info else _origin(versions.get("provider_origin")),
        provider_kind=info.kind if info else None,
        model=info.model if info else None,
        prompt_version=info.prompt_version if info else None,
        environment_kind=environment.kind if environment else _str(versions.get("environment")),
        environment_simulated=environment.is_simulated if environment else None,
        config_hash=_str(versions.get("config_hash")),
        code_version=_str(versions.get("code_version")),
        recap_version=_str(versions.get("recap")),
        started_at=started,
        ended_at=ended,
        wall_seconds=round((ended - started).total_seconds(), 3) if ended else None,
        rounds=len(record.strategies),
        agents=list(dict.fromkeys(f.agent_role.value for f in record.findings)),
        model_requests=record.usage.model_requests,
        oracle=oracle,
        replay=replay,
        export=(exports or {}).get(record.run_id),
        recording=file_id(own_recording),
        episode_id=record.episode_id,
        error=record.error,
        labels=([PUBLISHED_LABEL] if published else []) + run_labels(record, replay),
        not_recorded=missing,
        published=published,
        interruption=record.interruption,
    )


# --- the Redis job queue (M6) ---------------------------------------------------------------

JOB_STATUSES: frozenset[str] = frozenset(get_args(JobStatus))


def job_view(job: Job, *, position: int | None, worker_alive: bool | None) -> JobView:
    """One job, as the queue holds it; its run's record says how the incident went."""
    running = job.status == "running"
    return JobView(
        job_id=job.job_id,
        status=job.status if job.status in JOB_STATUSES else "failed",
        run_id=job.run_id,
        runs=list(job.runs),
        scenario_id=job.scenario_id,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        position=position,
        worker=(job.worker or None) if running else None,
        worker_alive=worker_alive if running else None,
        submitted_at=_when(job.submitted_at),
        claimed_at=_when(job.claimed_at),
        finished_at=_when(job.finished_at),
        category=job.category or None,
        detail=job.detail or None,
        cancel_requested=job.cancel_requested,
        provider=_str(job.config.get("provider")),
        replay_fixture=_str(job.config.get("replay_fixture")),
    )


def worker_view(worker_id: str, heartbeat: Mapping[str, Any] | None, jobs: list[str]) -> WorkerView:
    """One worker, from its heartbeat (only what it wrote about itself; nothing secret)."""
    beat = heartbeat or {}
    return WorkerView(
        worker_id=worker_id,
        alive=heartbeat is not None,
        host=_str(beat.get("host")),
        pid=_int(beat.get("pid")),
        started_at=_when(beat.get("started_at")),
        concurrency=_int(beat.get("concurrency")),
        target=_str(beat.get("target")),
        jobs=jobs,
    )


def queue_view(
    backend: ExecutionBackend,
    *,
    running: int,
    config: QueueConfig | None = None,
    snapshot: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> QueueView:
    """GET /queue: the in-process backend, or the Redis queue as one snapshot read it."""
    if backend == "inprocess" or config is None:
        return QueueView(
            backend="inprocess",
            reachable=True,
            address=None,
            error=None,
            depth=None,
            capacity=None,
            max_attempts=None,
            heartbeat_seconds=None,
            heartbeat_ttl_seconds=None,
            running=running,
            workers=[],
            jobs=[],
            counters={},
        )
    snap = snapshot or {}
    waiting: list[str] = list(snap.get("waiting", []))
    workers = [
        worker_view(w["worker_id"], w["heartbeat"], w["jobs"]) for w in snap.get("workers", [])
    ]
    alive = {w.worker_id for w in workers if w.alive}
    jobs = [
        job_view(
            job,
            position=len(waiting) - waiting.index(job.job_id) if job.job_id in waiting else None,
            worker_alive=job.worker in alive,
        )
        for job in snap.get("jobs", [])
    ]
    return QueueView(
        backend="redis",
        reachable=snapshot is not None,
        address=RedisAddress.parse(config.url).shown(),
        error=error,
        depth=snap.get("depth"),
        capacity=config.capacity,
        max_attempts=config.max_attempts,
        heartbeat_seconds=config.heartbeat_seconds,
        heartbeat_ttl_seconds=config.heartbeat_ttl_seconds,
        running=sum(len(w.jobs) for w in workers),
        workers=workers,
        jobs=jobs,
        counters=dict(snap.get("counters", {})),
    )


def _when(value: object) -> datetime | None:
    """An ISO timestamp the queue stored; None when absent or unreadable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class RunFilters:
    """GET /runs filters; each one matches exactly, and an unset one matches everything."""

    provider_class: str | None = None
    provider: str | None = None  # the provider id ("ollama:qwen3:8b") or the model name
    scenario: str | None = None
    state: str | None = None
    environment: str | None = None
    label: str | None = None

    def match(self, record: RunRecord) -> bool:
        info = record.provider_info
        origin = info.origin if info else record.versions.get("provider_origin")
        kind = record.environment.kind if record.environment else record.versions.get("environment")
        model = info.model if info else None
        checks = (
            (self.provider_class, origin),
            (self.scenario, record.scenario_id),
            (self.state, record.state.value),
            (self.environment, kind),
            (self.label, record.config.label),
        )
        if any(wanted is not None and wanted != actual for wanted, actual in checks):
            return False
        return self.provider is None or self.provider in (record.provider, model)


# ======================================================================================
# Timeline
# ======================================================================================


def event_rounds(events: Sequence[TraceEvent]) -> dict[int, int | None]:
    """The investigation round each event belongs to, by its sequence number.

    A round starts at its routing decision and lasts while the run is INVESTIGATING; agent
    events name their round themselves. Events outside an investigation get None.
    """
    rounds: dict[int, int | None] = {}
    current: int | None = None
    for event in events:
        payload = event.payload
        if event.category == C.ROUTING:
            current = _int(payload.get("round"))
        elif event.category == C.STATE and payload.get("to") == IncidentState.INVESTIGATING:
            current = None  # a new epoch: its first routing decision names the round
        own = _int(payload.get("round")) if event.category in _ROUND_IN_PAYLOAD else None
        investigating = event.stage == IncidentState.INVESTIGATING
        rounds[event.seq] = own if own is not None else (current if investigating else None)
    return rounds


def _server_wait(server: float | None, compute: float | None) -> float | None:
    """Time a request waited inside the model server: its time there minus its compute."""
    if server is None or compute is None:
        return None
    return round(max(0.0, server - compute), 1)


def model_request(payload: Mapping[str, Any]) -> ModelRequest | None:
    """An `llm.call` payload as a typed request (None when it is not a valid one)."""
    duration = _float(payload.get("duration_ms"))
    server = _float(payload.get("server_ms"))
    compute = _float(payload.get("server_compute_ms"))
    attempt = _int(payload.get("attempt")) or 1
    origin = _origin(payload.get("origin"))
    if origin is None or duration is None:
        return None
    return ModelRequest(
        provider=str(payload.get("provider") or ""),
        model=str(payload.get("model") or ""),
        model_reported=_str(payload.get("model_reported")),
        origin=origin,
        purpose=str(payload.get("purpose") or ""),
        mode=str(payload.get("mode") or ""),
        attempt=attempt,
        retry=attempt > 1,
        status=_int(payload.get("status")),
        ok=bool(payload.get("ok")),
        response_kind=_str(payload.get("response_kind")),
        input_tokens=_int(payload.get("input_tokens")),
        output_tokens=_int(payload.get("output_tokens")),
        duration_ms=duration,
        server_ms=server,
        outside_server_ms=round(duration - server, 1) if server is not None else None,
        server_compute_ms=compute,
        server_wait_ms=_server_wait(server, compute),
        replayed=bool(payload.get("replayed")),
        finish_reason=_str(payload.get("finish_reason")),
    )


def timeline_entry(event: TraceEvent, round_: int | None) -> TimelineEntry:
    """One stored event as a timeline entry (its payload is passed on as stored)."""
    payload = event.payload
    category = event.category
    return TimelineEntry(
        seq=event.seq,
        ts=event.ts,
        stage=event.stage,
        category=category,
        kind=_KIND.get(category, "other"),
        agent=event.agent,
        round=round_,
        step=_int(payload.get("step")),
        tool=_str(payload.get("tool")),
        invocation_id=_str(payload.get("invocation_id")),
        verdict=_verdict(category, payload),
        ok=_ok(category, payload),
        evidence_ids=_event_evidence_ids(category, payload),
        duration_ms=event.duration_ms,
        model_request=model_request(payload) if category == C.LLM_CALL else None,
        untrusted_text=category in _UNTRUSTED_TEXT
        or (category == C.STATE and "incident" in payload),
        payload=dict(payload),
    )


def timeline_view(
    run_id: str,
    record: RunRecord | None,
    events: Sequence[TraceEvent],
    status: RunStatus,
    *,
    after_seq: int = 0,
    limit: int = 500,
    filters: TimelineFilters | None = None,
    local_model_providers: Collection[str] = (),
) -> TimelineView:
    """One page of a run's trace (after `after_seq`, at most `limit` entries), plus the
    run's lanes, rounds and counts computed over the whole trace.

    `local_model_providers` names the configured providers served by a local Ollama server
    (for the note on requests waiting for each other).
    """
    filters = filters or TimelineFilters()
    rounds_by_seq = event_rounds(events)
    matching = [e for e in events if _passes(e, rounds_by_seq[e.seq], filters)]
    after = [e for e in matching if e.seq > after_seq]
    page = after[:limit]
    calls = [e for e in events if e.category == C.LLM_CALL]
    rounds = round_infos(record, events, rounds_by_seq)
    notes: list[str] = []
    if any(r.mode == "parallel" for r in rounds) and any(
        e.payload.get("provider") in local_model_providers for e in calls
    ):
        notes.append(CONCURRENCY_NOTE)
    if any(e.payload.get("replayed") for e in calls):
        notes.append(REPLAY_NOTE)
    missing: list[str] = []
    if not events:
        missing.append("trace events")
    if calls and all(e.payload.get("server_ms") is None for e in calls):
        missing.append("server_ms (model server time)")
    return TimelineView(
        run_id=run_id,
        status=status,
        state=record.state if record else None,
        live=status == "running",
        trace_available=bool(events),
        total_events=len(events),
        after_seq=after_seq,
        limit=limit,
        filters=filters,
        matching_events=len(matching),
        next_after_seq=page[-1].seq if page else after_seq,
        has_more=len(after) > limit,
        entries=[timeline_entry(e, rounds_by_seq[e.seq]) for e in page],
        lanes=agent_lanes(record, events, rounds_by_seq, live=status == "running", rounds=rounds),
        rounds=rounds,
        counts=dict(Counter(_KIND.get(e.category, "other") for e in events)),
        retries=RetryCounts(
            model_retries=sum(1 for e in calls if (_int(e.payload.get("attempt")) or 1) > 1),
            failed_model_requests=sum(1 for e in calls if not e.payload.get("ok")),
            fix_attempts=sum(1 for e in events if e.category == C.MITIGATION),
        ),
        notes=notes,
        not_recorded=missing,
    )


def agent_lanes(
    record: RunRecord | None,
    events: Sequence[TraceEvent],
    rounds_by_seq: Mapping[int, int | None],
    *,
    live: bool,
    rounds: Sequence[RoundInfo] | None = None,
) -> list[AgentLane]:
    """Each agent's turn in each round: from its finding when it has one, else from its
    `agent.start` event (a turn still running, or one the run's end cut off). `rounds` are
    the run's round_infos when the caller has them already."""
    request_ms: dict[tuple[str, int], float] = {}
    server_ms: dict[tuple[str, int], float] = {}
    for event in events:
        round_ = rounds_by_seq.get(event.seq)
        if event.category != C.LLM_CALL or event.agent is None or round_ is None:
            continue
        key = (event.agent, round_)
        request_ms[key] = request_ms.get(key, 0.0) + (_float(event.payload.get("duration_ms")) or 0)
        server = _float(event.payload.get("server_ms"))
        if server is not None:
            server_ms[key] = server_ms.get(key, 0.0) + server
    if rounds is None:
        rounds = round_infos(record, events, rounds_by_seq)
    modes = {r.round: r.mode for r in rounds}
    lanes: list[AgentLane] = []
    seen: set[tuple[str, int]] = set()
    for finding in record.findings if record else []:
        key = (finding.agent_role.value, finding.round)
        seen.add(key)
        usage = finding.usage
        lanes.append(
            AgentLane(
                agent=key[0],
                round=finding.round,
                mode=modes.get(finding.round),
                started_at=finding.started_at,
                finished_at=finding.finished_at,
                duration_ms=_ms(finding.finished_at - finding.started_at),
                outcome=finding.outcome or ("error" if finding.error else ""),
                partial=finding.partial,
                error=finding.error,
                steps=usage.steps,
                tool_calls=usage.tool_calls,
                model_requests=usage.model_requests,
                input_tokens=usage.actual_input_tokens,
                output_tokens=usage.actual_output_tokens,
                model_request_ms=_rounded(request_ms.get(key)),
                model_server_ms=_rounded(server_ms.get(key)),
                evidence_ids=list(finding.evidence_ids),
                hypotheses=len(finding.hypotheses),
            )
        )
    for event in events:
        if event.category != C.AGENT_START:
            continue
        role = _str(event.payload.get("role")) or event.agent
        round_ = rounds_by_seq.get(event.seq)
        if role is None or round_ is None or (role, round_) in seen:
            continue
        seen.add((role, round_))
        lanes.append(
            AgentLane(
                agent=role,
                round=round_,
                mode=modes.get(round_),
                started_at=event.ts,
                finished_at=None,
                duration_ms=None,
                outcome="running" if live else "unfinished",
                partial=False,
                error=None,
                steps=None,
                tool_calls=None,
                model_requests=None,
                input_tokens=None,
                output_tokens=None,
                model_request_ms=_rounded(request_ms.get((role, round_))),
                model_server_ms=_rounded(server_ms.get((role, round_))),
                evidence_ids=[],
                hypotheses=None,
            )
        )
    return sorted(lanes, key=lambda lane: (lane.round, lane.agent))


def round_infos(
    record: RunRecord | None,
    events: Sequence[TraceEvent],
    rounds_by_seq: Mapping[int, int | None],
) -> list[RoundInfo]:
    """Every investigation round, from its trace events (or, without a trace, the record)."""
    rounds: dict[int, RoundInfo] = {}
    for event in events:
        payload = event.payload
        number = rounds_by_seq.get(event.seq)
        if event.category == C.ROUTING and number is not None:
            rounds[number] = RoundInfo(
                round=number,
                mode=str(payload.get("mode") or ""),
                rule_id=str(payload.get("rule_id") or ""),
                reason=str(payload.get("reason") or ""),
                agents=_strs(payload.get("agents")),
                escalation=None,
                wall_ms=None,
                sum_ms=None,
                overlap_ms=None,
                model_request_ms=None,
                verification_status=None,
            )
            continue
        current = rounds.get(number) if number is not None else None
        if current is None:
            continue
        if event.category == C.INVESTIGATION_ROUND:
            current.focus_services = _strs(payload.get("focus_services"))
            current.open_questions = _strs(payload.get("open_questions"))
            current.leads = _strs(payload.get("leads"))
            current.escalation = _bool(payload.get("escalation"))
        elif event.category == C.ORCHESTRATION and "agents" in payload:
            current.wall_ms = _float(payload.get("wall_ms"))
            current.sum_ms = _float(payload.get("sum_ms"))
            current.overlap_ms = _float(payload.get("overlap_ms"))
        elif event.category == C.VERIFY_EVIDENCE and "checks" in payload:
            current.verification_status = _str(payload.get("status"))
        elif event.category == C.LLM_CALL:
            spent = _float(payload.get("duration_ms")) or 0.0
            current.model_request_ms = round((current.model_request_ms or 0.0) + spent, 1)
    if not rounds and record is not None:  # no trace: what the checkpoint kept
        for decision in record.strategies:
            rounds[decision.round] = RoundInfo(
                round=decision.round,
                mode=decision.mode.value,
                rule_id=decision.rule_id,
                reason=decision.reason,
                agents=[a.value for a in decision.selected_agents],
                escalation=decision.signals.escalation,
                wall_ms=None,
                sum_ms=None,
                overlap_ms=None,
                model_request_ms=None,
                verification_status=None,
            )
    return [rounds[n] for n in sorted(rounds)]


def _passes(event: TraceEvent, round_: int | None, filters: TimelineFilters) -> bool:
    if filters.agent is not None and event.agent != filters.agent:
        return False
    if filters.round is not None and round_ != filters.round:
        return False
    if filters.kind is not None and _KIND.get(event.category, "other") != filters.kind:
        return False
    return filters.tool is None or event.payload.get("tool") == filters.tool


def _verdict(category: str, payload: Mapping[str, Any]) -> str | None:
    if category == C.POLICY:
        return _str(payload.get("verdict"))
    if category == C.APPROVAL_DECIDED:
        return _str(payload.get("decision"))
    if category == C.VERIFY_EVIDENCE:
        if isinstance(payload.get("proposal_check"), Mapping):
            return "proposal_ok" if payload["proposal_check"].get("ok") else "proposal_refused"
        return _str(payload.get("status"))
    if category == C.VERIFY_RECOVERY:
        return "verified" if payload.get("verified") else "not_verified"
    return None


def _ok(category: str, payload: Mapping[str, Any]) -> bool | None:
    if category in (C.TOOL_RESULT, C.LLM_CALL, C.MITIGATION):
        return _bool(payload.get("ok"))
    if category == C.VERIFY_RECOVERY:
        return _bool(payload.get("verified"))
    return None


def _event_evidence_ids(category: str, payload: Mapping[str, Any]) -> list[str]:
    if category in (C.TOOL_RESULT, C.MITIGATION, C.INJECTION):
        found = _str(payload.get("evidence_id"))
        return [found] if found else []
    if category == C.AGENT_FINISH:
        return _strs(payload.get("evidence_ids"))
    if category == C.VERIFY_EVIDENCE:
        checks = payload.get("checks")
        ids = [i for c in _dicts(checks) for i in _strs(c.get("basis_evidence_ids"))]
        return list(dict.fromkeys(ids))
    if category == C.PROPOSAL and isinstance(payload.get("proposal"), Mapping):
        return _strs(payload["proposal"].get("evidence_ids"))
    return []


# ======================================================================================
# Evidence and decisions
# ======================================================================================


def trust_class(evidence: Evidence) -> TrustClass:
    """How far an evidence item can be trusted, by where its text came from."""
    if evidence.trust_level == TrustLevel.TRUSTED:
        return "recap_trusted_text"
    return _TRUST_BY_SOURCE.get(evidence.source_type, "untrusted_tool_output")


def evidence_view(
    run_id: str,
    record: RunRecord | None,
    evidence: Sequence[Evidence],
    events: Sequence[TraceEvent],
    status: RunStatus,
    *,
    registry_recorded: bool = True,
) -> EvidenceView:
    """The evidence registry, every claim with its citations, and every verdict on them.

    `registry_recorded=False` (a published run): the registry's contents were not exported,
    so a cited ID missing from it is not a finding, and the view says what is not recorded."""
    author = text_author(record)
    known = {item.id for item in evidence}
    rounds_by_seq = event_rounds(events)
    evidence_round = {
        str(e.payload.get("evidence_id")): rounds_by_seq.get(e.seq)
        for e in events
        if e.category == C.TOOL_RESULT and e.payload.get("evidence_id")
    }
    findings = [_finding_view(f, author, known) for f in (record.findings if record else [])]
    claims = [claim for finding in findings for claim in finding.claims]
    verifications = [
        _verification_from_event(e, rounds_by_seq.get(e.seq))
        for e in events
        if e.category == C.VERIFY_EVIDENCE and "checks" in e.payload
    ]
    final = _final_report(record)
    basis = {
        i
        for v in [*verifications, *([final] if final else [])]
        for c in v.checks
        for i in c.basis_evidence_ids
    }
    registry = [
        EvidenceItem(
            id=item.id,
            source_type=item.source_type,
            source_ref=item.source_ref,
            tool=item.tool_name,
            timestamp=item.timestamp,
            recorded_by=item.agent_role.value if item.agent_role else None,
            round=evidence_round.get(item.id),
            trust_class=trust_class(item),
            trust_level=item.trust_level,
            flags=list(item.flags),
            token_estimate=item.token_estimate,
            content=item.content,
            supports=[c.claim_id for c in claims if item.id in c.supporting_evidence_ids],
            contradicts=[c.claim_id for c in claims if item.id in c.contradicting_evidence_ids],
            in_rule_basis=item.id in basis,
        )
        for item in evidence
    ]
    proposals = [
        ProposalView(
            proposal_id=p.proposal_id,
            action=p.action,
            target=p.target,
            params=dict(p.params),
            digest=p.digest(),
            author=author,
            source=p.source,
            rationale=p.rationale,
            evidence_ids=list(p.evidence_ids),
            citations=list(p.citations),
            declared_risk=p.risk_level.value,
        )
        for p in (record.proposals if record else [])
    ]
    cited = [i for c in claims for i in (*c.supporting_evidence_ids, *c.contradicting_evidence_ids)]
    cited += [i for p in proposals for i in p.evidence_ids]
    cited += [
        i for v in [*verifications, *([final] if final else [])] for i in v.unknown_evidence_ids
    ]
    unknown = sorted({i for i in cited if i not in known}) if registry_recorded else []
    missing = [] if events else ["trace events (verification history, proposal checks, flags)"]
    if not registry_recorded:
        missing.append(
            "the evidence registry's contents (the tool outputs the agents read): not published "
            "with this run; each evidence ID and the tool that produced it are in the trace"
        )
    if record is None:
        missing.append("run record (findings, proposals, policy verdicts)")
    return EvidenceView(
        run_id=run_id,
        status=status,
        state=record.state if record else None,
        trace_available=bool(events),
        registry=registry,
        findings=findings,
        verifications=verifications,
        final_report=final,
        proposals=proposals,
        proposal_checks=[
            ProposalCheckView(
                seq=e.seq,
                proposal_id=str(e.payload.get("proposal_id") or ""),
                ok=bool(e.payload["proposal_check"].get("ok")),
                problems=_strs(e.payload["proposal_check"].get("problems")),
                rule_ids=_strs(e.payload["proposal_check"].get("rule_ids")),
                enforced=bool(e.payload.get("enforced", True)),
            )
            for e in events
            if e.category == C.VERIFY_EVIDENCE
            and isinstance(e.payload.get("proposal_check"), Mapping)
        ],
        policy_verdicts=[
            PolicyVerdictView(
                tool=d.tool_name,
                verdict=d.verdict.value,
                rule_ids=list(d.rule_ids),
                reasons=list(d.reasons),
                risk=d.risk.value if d.risk else None,
                caller=d.caller.value if d.caller else None,
                decided_at=d.decided_at,
            )
            for d in (record.safety_decisions if record else [])
        ],
        injection_flags=[
            InjectionFlagView(
                seq=e.seq,
                evidence_id=str(e.payload.get("evidence_id") or ""),
                tool=_str(e.payload.get("tool")),
                agent=e.agent,
                patterns=_strs(e.payload.get("patterns")),
            )
            for e in events
            if e.category == C.INJECTION
        ],
        unknown_cited_ids=unknown,
        trust_classes=dict(TRUST_CLASSES),
        not_recorded=missing,
    )


def _finding_view(finding: Any, author: TextAuthor, known: set[str]) -> FindingView:
    agent = finding.agent_role.value
    claims = [
        ClaimView(
            claim_id=f"r{finding.round}.{agent}.{index}",
            round=finding.round,
            agent=agent,
            author=author,
            claim=h.claim,
            mechanism=h.mechanism,
            target_service=h.target_service,
            details=dict(h.details),
            origin=h.origin,
            supporting_evidence_ids=list(h.supporting_evidence_ids),
            contradicting_evidence_ids=list(h.contradicting_evidence_ids),
            unknown_evidence_ids=[
                i
                for i in dict.fromkeys([*h.supporting_evidence_ids, *h.contradicting_evidence_ids])
                if i not in known
            ],
            confidence_note=h.confidence_note,
            partial=finding.partial,
        )
        for index, h in enumerate(finding.hypotheses, start=1)
    ]
    return FindingView(
        agent=agent,
        round=finding.round,
        outcome=finding.outcome or ("error" if finding.error else ""),
        partial=finding.partial,
        error=finding.error,
        author=author,
        summary=finding.summary,
        evidence_ids=list(finding.evidence_ids),
        doc_refs=list(finding.doc_refs),
        claims=claims,
        started_at=finding.started_at,
        finished_at=finding.finished_at,
    )


def _verification_from_event(event: TraceEvent, round_: int | None) -> VerificationView:
    payload = event.payload
    best = payload.get("best")
    return VerificationView(
        source="trace",
        seq=event.seq,
        round=round_,
        status=str(payload.get("status") or ""),
        summary=str(payload.get("summary") or ""),
        best=BestClaim(
            claim=str(best.get("claim") or ""),
            mechanism=str(best.get("mechanism") or ""),
            target_service=_str(best.get("target_service")),
        )
        if isinstance(best, Mapping)
        else None,
        checks=[
            CheckView(
                claim=str(c.get("claim") or ""),
                mechanism=str(c.get("mechanism") or ""),
                target=_str(c.get("target")),
                status=str(c.get("status") or ""),
                proposers=_strs(c.get("proposers")),
                basis_evidence_ids=_strs(c.get("basis_evidence_ids")),
                detail=str(c.get("detail") or ""),
                passed=_strs(c.get("passed")),
                failed=_strs(c.get("failed")),
                rule_ids=_strs(c.get("rule_ids")),
            )
            for c in _dicts(payload.get("checks"))
        ],
        unknown_evidence_ids=_strs(payload.get("unknown_evidence_ids")),
        focus_services=_strs(payload.get("focus_services")),
        disagreements=_disagreements(payload.get("disagreements")),
        leads=_strs(payload.get("leads")),
    )


def _final_report(record: RunRecord | None) -> VerificationView | None:
    report = record.verification if record else None
    if record is None or report is None:
        return None
    best = report.best
    return VerificationView(
        source="run_record",
        seq=None,
        round=record.strategies[-1].round if record.strategies else None,
        status=report.status,
        summary=report.summary,
        best=BestClaim(
            claim=best.claim, mechanism=best.mechanism.value, target_service=best.target_service
        )
        if best
        else None,
        checks=[
            CheckView(
                claim=c.hypothesis.claim,
                mechanism=c.hypothesis.mechanism.value,
                target=c.hypothesis.target_service,
                status=c.hypothesis.status.value,
                proposers=[p.value for p in c.proposers],
                basis_evidence_ids=list(c.basis_evidence_ids),
                detail=c.detail,
                passed=list(c.passed),
                failed=list(c.failed),
                rule_ids=list(c.rule_ids),
                supporting_evidence_ids=list(c.hypothesis.supporting_evidence_ids),
                contradicting_evidence_ids=list(c.hypothesis.contradicting_evidence_ids),
            )
            for c in report.checks
        ],
        unknown_evidence_ids=list(report.unknown_evidence_ids),
        focus_services=list(report.focus_services),
        disagreements=list(report.disagreements),
        leads=list(report.leads),
    )


def _disagreements(value: Any) -> list[Disagreement]:
    """Recorded disagreements; an entry that no longer validates is left out, not guessed."""
    found = []
    for item in _dicts(value):
        try:
            found.append(Disagreement.model_validate(item))
        except ValidationError:
            continue
    return found


# ======================================================================================
# Retrieval and memory
# ======================================================================================


def retrieval_view(
    run_id: str, record: RunRecord | None, events: Sequence[TraceEvent], status: RunStatus
) -> RetrievalView:
    """Runbook searches with their results and citations, and memory searches with RECAP's
    reuse verdicts, exactly as the trace and the checkpoint recorded them."""
    author = text_author(record)
    rounds_by_seq = event_rounds(events)
    findings = record.findings if record else []
    proposal_citations = {c for p in (record.proposals if record else []) for c in p.citations}

    def cited_by(chunk_id: str) -> list[str]:
        return list(dict.fromkeys(f.agent_role.value for f in findings if chunk_id in f.doc_refs))

    def result(item: Mapping[str, Any]) -> SearchResultView:
        chunk_id = str(item.get("chunk_id") or "")
        return SearchResultView(
            chunk_id=chunk_id,
            doc_path=_str(item.get("doc_path")),
            heading=_str(item.get("heading")),
            rank=_int(item.get("rank")),
            score=_float(item.get("score")),
            retriever=_str(item.get("retriever")),
            signals=_signals(item.get("signals")),
            cited_by_agents=cited_by(chunk_id),
            cited_by_proposal=chunk_id in proposal_citations,
        )

    searches = []
    for event in events:
        if event.category != C.RETRIEVAL:
            continue
        payload = event.payload
        stage = str(payload.get("stage") or "")
        embedding = payload.get("embedding")
        searches.append(
            SearchView(
                seq=event.seq,
                ts=event.ts,
                stage=stage,
                reason=str(payload.get("reason") or ""),
                query=str(payload.get("query") or ""),
                # The alert's and the proposal's queries are joined by RECAP's code (the
                # alert's words are still the alert's); an agent's search is its own text.
                query_author=author if stage == "agent" else "recap",
                agent=event.agent,
                mode=_str(payload.get("mode")),
                retriever=_str(payload.get("retriever")),
                embedding=EmbeddingCallView(
                    model=_str(embedding.get("model")),
                    inputs=_int(embedding.get("inputs")),
                    status=_int(embedding.get("status")),
                    input_tokens=_int(embedding.get("input_tokens")),
                    duration_ms=_float(embedding.get("duration_ms")),
                    replayed=_bool(embedding.get("replayed")),
                )
                if isinstance(embedding, Mapping)
                else None,
                results=[result(item) for item in _dicts(payload.get("results"))],
            )
        )
    info = record.retrieval if record else None
    missing = ["search duration (only a hybrid search's embedding request is timed)"]
    if not events:
        missing.append("trace events (searches, memory assessments)")
    if info is None:
        missing.append("retrieval info (mode, embedder, embedding requests)")
    elif info.mode == "hybrid":
        # The run names its query embedder and what its server reported, not which stored
        # vector file it searched (GET /system shows the one loaded now).
        missing.append("vector index identity (the stored runbook vectors searched)")
    config = record.config if record else None
    return RetrievalView(
        run_id=run_id,
        status=status,
        state=record.state if record else None,
        trace_available=bool(events),
        use_rag=config.use_rag if config else False,
        mode=info.mode if info else None,
        embedder=info.embedder if info else None,
        embedding_server=dict(info.server) if info and info.server else None,
        embedding_requests=info.embedding_requests if info else None,
        embedding_input_tokens=info.embedding_input_tokens if info else None,
        replayed_embedding_requests=info.replayed_requests if info else None,
        agent_searches=info.agent_searches if info else None,
        agent_search_refusals=[
            RefusedSearchView(seq=e.seq, agent=e.agent, error=_str(e.payload.get("error")))
            for e in events
            if e.category == C.TOOL_RESULT
            and e.payload.get("tool") == "search_runbooks"
            and not e.payload.get("ok")
        ],
        knowledge_overlays=_strs(record.versions.get("knowledge_overlays")) if record else [],
        searches=searches,
        skipped=[
            SkipView(
                seq=e.seq,
                stage=_str(e.payload.get("stage")),
                reason=str(e.payload.get("reason") or ""),
            )
            for e in events
            if e.category == C.RETRIEVAL_SKIP
        ],
        context_docs=[
            _chunk_result(d, cited_by(d.chunk.chunk_id), proposal_citations)
            for d in (record.retrieved_docs if record else [])
        ],
        memory=_memory_view(record, events, rounds_by_seq),
        not_recorded=missing,
    )


def _chunk_result(
    doc: RetrievedChunk, cited_by: list[str], proposal_citations: set[str]
) -> SearchResultView:
    return SearchResultView(
        chunk_id=doc.chunk.chunk_id,
        doc_path=doc.chunk.doc_path,
        heading=doc.chunk.heading,
        rank=doc.rank,
        score=round(doc.score, 4),
        retriever=doc.retriever,
        signals=dict(doc.signals),
        cited_by_agents=cited_by,
        cited_by_proposal=doc.chunk.chunk_id in proposal_citations,
    )


def _memory_view(
    record: RunRecord | None, events: Sequence[TraceEvent], rounds_by_seq: Mapping[int, int | None]
) -> MemoryRetrievalView:
    config = record.config if record else None
    written = next((e for e in events if e.category == C.MEMORY_WRITE), None)
    return MemoryRetrievalView(
        enabled=config.use_memory if config else False,
        read_partitions=[p.value for p in (config.memory_read_partitions or [])] if config else [],
        write_partition=config.memory_write_partition.value
        if config and config.memory_write_partition
        else None,
        write_enabled=config.memory_write if config else False,
        searches=[
            MemorySearchView(
                seq=e.seq,
                readable_partitions=_strs(e.payload.get("readable_partitions")),
                created_before=_str(e.payload.get("created_before")),
                semantic=_str(e.payload.get("semantic")),
                hits=[_hit_from_payload(h) for h in _dicts(e.payload.get("hits"))],
            )
            for e in events
            if e.category == C.MEMORY_SEARCH
        ],
        assessments=[
            MemoryAssessmentView(
                seq=e.seq,
                round=rounds_by_seq.get(e.seq),
                hits=[_hit_from_payload(h) for h in _dicts(e.payload.get("hits"))],
                restore_points=e.payload.get("restore_points"),
            )
            for e in events
            if e.category == C.MEMORY_ASSESS
        ],
        final_hits=[_hit_from_record(h) for h in (record.memory_hits if record else [])],
        written=MemoryWriteView(
            episode_id=str(written.payload.get("episode_id") or ""),
            partition=_str(written.payload.get("partition")),
            outcome=_str(written.payload.get("outcome")),
            verified=_bool(written.payload.get("verified")),
        )
        if written is not None
        else None,
        skipped=[str(e.payload.get("reason") or "") for e in events if e.category == C.MEMORY_SKIP],
    )


def _hit_from_payload(hit: Mapping[str, Any]) -> MemoryHitView:
    return MemoryHitView(
        episode_id=str(hit.get("episode_id") or ""),
        similarity=_float(hit.get("similarity")),
        signals=_signals(hit.get("signals")),
        outcome=_str(hit.get("outcome")),
        verified=_bool(hit.get("verified")),
        mechanism=_str(hit.get("mechanism")),
        service=_str(hit.get("service")),
        partition=_str(hit.get("partition")),
        safe_to_reuse=_bool(hit.get("safe_to_reuse")),
        mechanism_match=_bool(hit.get("mechanism_match")),
        reason=_str(hit.get("reason")),
        supporting=_strs(hit.get("supporting")),
        contradicting=_strs(hit.get("contradicting")),
    )


def _hit_from_record(hit: MemoryHit) -> MemoryHitView:
    episode = hit.episode
    assessed = hit.mechanism_match is not None or bool(hit.reason)
    return MemoryHitView(
        episode_id=episode.episode_id,
        similarity=hit.similarity,
        signals=dict(hit.signals),
        outcome=hit.outcome.value,
        verified=episode.verified,
        mechanism=episode.failure_mechanism.value if episode.failure_mechanism else None,
        service=episode.service,
        partition=episode.partition.value,
        safe_to_reuse=hit.safe_to_reuse if assessed else None,
        mechanism_match=hit.mechanism_match,
        reason=hit.reason or None,
        supporting=list(hit.supporting),
        contradicting=list(hit.contradicting),
    )


# ======================================================================================
# Usage
# ======================================================================================


def provider_limits(spec: ProviderSettings | None) -> ProviderLimitsView:
    """A provider's configured limits (no key, only numbers and dates)."""
    if spec is None:
        return ProviderLimitsView()
    llm = spec.kind == "llm"
    free = spec.free_tier
    return ProviderLimitsView(
        max_output_tokens=spec.max_output_tokens if llm else None,
        context_tokens=spec.context_tokens,
        request_timeout_seconds=spec.request_timeout_seconds if llm else None,
        max_attempts=spec.max_attempts if llm else None,
        run_budget_overrides=dict(spec.budget),
        free_daily_request_limit=dict(free.free_daily_request_limit) if free else None,
        limits_checked_on=free.limits_checked_on if free else None,
        caps=free.caps.model_dump() if free else None,
        reserved_output_tokens=free.reserved_output_tokens if free else None,
    )


def usage_view(
    run_id: str,
    record: RunRecord | None,
    events: Sequence[TraceEvent],
    status: RunStatus,
    *,
    providers: Mapping[str, ProviderSettings] | None = None,
    local_model_providers: Collection[str] = (),
) -> UsageView:
    """Requests, the servers' own token counts, RECAP's estimates and time, per model."""
    providers = providers or {}
    groups: dict[tuple[str, str, str], list[TraceEvent]] = {}
    for event in events:
        if event.category == C.LLM_CALL and _origin(event.payload.get("origin")) is not None:
            p = event.payload
            key = (str(p.get("provider") or ""), str(p.get("model") or ""), str(p.get("origin")))
            groups.setdefault(key, []).append(event)
    models = [
        _model_usage(provider, model, origin, calls, providers.get(provider))
        for (provider, model, origin), calls in groups.items()
    ]
    summary = next((e for e in reversed(events) if e.category == C.RUN_SUMMARY), None)
    usage = record.usage if record else None
    info = record.retrieval if record else None
    origin = record.provider_info.origin if record and record.provider_info else None
    rounds = round_infos(record, events, event_rounds(events))
    calls = [e for group in groups.values() for e in group]
    notes: list[str] = []
    if origin == "scripted":
        notes.append(SCRIPTED_NOTE)
    if any(e.payload.get("replayed") for e in calls):
        notes.append(REPLAY_NOTE)
    if any(r.mode == "parallel" for r in rounds) and any(
        e.payload.get("provider") in local_model_providers for e in calls
    ):
        notes.append(CONCURRENCY_NOTE)
    missing: list[str] = []
    if not events:
        missing.append("trace events (per-model requests)")
    if summary is None:
        missing.append("run summary (budget used, cost label)")
    if calls and all(e.payload.get("server_ms") is None for e in calls):
        missing.append("server_ms (model server time)")
    return UsageView(
        run_id=run_id,
        status=status,
        state=record.state if record else None,
        trace_available=bool(events),
        provider_class=origin,
        models=models,
        totals=UsageTotals(
            model_requests=usage.model_requests if usage else 0,
            input_tokens=usage.actual_input_tokens if usage else None,
            output_tokens=usage.actual_output_tokens if usage else None,
            unreported_requests=usage.unreported_calls if usage else 0,
            tool_calls=usage.tool_calls if usage else 0,
            steps=usage.steps if usage else 0,
        ),
        estimated_context_tokens=EstimatedTokens(tokens=usage.estimated_tokens if usage else 0),
        budget=BudgetView(
            limits=record.incident.budget.model_dump() if record and record.incident else None,
            used=dict(summary.payload["budget"])
            if summary is not None and isinstance(summary.payload.get("budget"), Mapping)
            else None,
        ),
        embedding=EmbeddingUsageView(
            requests=info.embedding_requests,
            input_tokens=info.embedding_input_tokens,
            replayed=info.replayed_requests,
        )
        if info is not None
        else None,
        cost_label=_str(summary.payload.get("cost")) if summary is not None else None,
        notes=notes,
        not_recorded=missing,
    )


def _model_usage(
    provider: str,
    model: str,
    origin: str,
    calls: Sequence[TraceEvent],
    spec: ProviderSettings | None,
) -> ModelUsageView:
    payloads = [e.payload for e in calls]
    inputs = [_int(p.get("input_tokens")) for p in payloads]
    outputs = [_int(p.get("output_tokens")) for p in payloads]
    durations = [_float(p.get("duration_ms")) or 0.0 for p in payloads]
    timed = [
        (d, s)
        for d, s in zip(durations, (_float(p.get("server_ms")) for p in payloads), strict=True)
        if s is not None
    ]
    split = [
        (s, c)
        for s, c in (
            (_float(p.get("server_ms")), _float(p.get("server_compute_ms"))) for p in payloads
        )
        if s is not None and c is not None
    ]
    replayed = sum(1 for p in payloads if p.get("replayed"))
    return ModelUsageView(
        provider=provider,
        model=model,
        provider_class=origin,  # checked by _origin when grouping
        requests=len(payloads),
        ok_requests=sum(1 for p in payloads if p.get("ok")),
        failed_requests=sum(1 for p in payloads if not p.get("ok")),
        retries=sum(1 for p in payloads if (_int(p.get("attempt")) or 1) > 1),
        replayed_requests=replayed,
        input_tokens=_sum_reported(inputs),
        output_tokens=_sum_reported(outputs),
        requests_without_token_counts=sum(
            1 for i, o in zip(inputs, outputs, strict=True) if i is None and o is None
        ),
        request_ms=round(sum(durations), 1),
        server_ms=round(sum(s for _, s in timed), 1) if timed else None,
        requests_with_server_ms=len(timed),
        outside_server_ms=round(sum(d - s for d, s in timed), 1) if timed else None,
        server_compute_ms=round(sum(c for _, c in split), 1) if split else None,
        server_wait_ms=round(sum(max(0.0, s - c) for s, c in split), 1) if split else None,
        by_purpose=dict(Counter(str(p.get("purpose") or "") for p in payloads)),
        by_agent=dict(Counter(e.agent or "controller" for e in calls)),
        # The current configuration describes live requests only: a replay's original
        # limits are in its recording, and this run sent nothing.
        limits=provider_limits(spec) if spec is not None and replayed < len(payloads) else None,
    )


# ======================================================================================
# Providers, system, replays, evaluations
# ======================================================================================

# The catalog's fields (recap/providers/catalog.py) that GET /providers has always returned.
CATALOG_FIELDS = (
    "name",
    "kind",
    "origin",
    "model",
    "mode",
    "description",
    "default",
    "status",
    "detail",
    "usage",
    "base_url",
    "think",
    "context_tokens",
    "installed",
    "key_env",
    "key_present",
    "opt_in_env",
    "opted_in",
    "attestation_env",
    "confirmed_on",
    "confirmation_age_days",
    "confirmation_fresh",
    "allowed_models",
    "free_daily_request_limit",
    "limits_checked_on",
    "caps",
    "last_rate_limit",
    "stopped",
    "recording",
    "replay_of",
)


def provider_view(
    entry: Mapping[str, Any],
    spec: ProviderSettings | None,
    selectable: bool,
    rule: str,
) -> ProviderView:
    """One catalog entry plus its class, limits, honest limitations and per-run rule."""
    origin = str(entry["origin"])
    return ProviderView(
        **{key: entry[key] for key in CATALOG_FIELDS if key in entry},
        provider_class=origin,  # ProviderSettings checks origins
        key_required=origin in ("free_hosted_llm", "paid_llm"),
        selectable=selectable,
        selection_rule=rule,
        limits=provider_limits(spec),
        limitations=list(PROVIDER_LIMITATIONS.get(origin, [])),
    )


def system_view(
    summary: Mapping[str, Any],
    *,
    replay_fixtures: int,
    launchable_fixtures: int,
    per_run_providers: list[str],
    api_schema: str,
) -> SystemView:
    """GET /system from the runtime's own summary (recap/runtime.py) plus the API's rules."""
    environment = EnvironmentInfo.model_validate(summary["environment"])
    features = dict(summary["features"])
    memory_access = {
        origin: MemoryAccessView.model_validate(rule)
        for origin, rule in features.pop("memory_access").items()
    }
    return SystemView(
        environment=environment,
        environment_kind=summary["environment_kind"],
        tool_transport=summary["tool_transport"],
        provider=dict(summary["provider"]),
        providers_available=list(summary["providers_available"]),
        retriever=summary["retriever"],
        retrieval_mode=summary["retrieval_mode"],
        hybrid_ready=summary["hybrid_ready"],
        embedder=summary["embedder"],
        knowledge_chunks=summary["knowledge_chunks"],
        router=summary["router"],
        safety_policy=summary["safety_policy"],
        verifier=summary["verifier"],
        oracle=summary["oracle"],
        memory_episodes=summary["memory_episodes"],
        trace_sinks=list(summary["trace_sinks"]),
        config_hash=summary["config_hash"],
        fixture_hash=summary["fixture_hash"],
        code_version=summary["code_version"],
        label=environment_label(environment),
        simulation_notice=SIMULATION_NOTICE if environment.is_simulated else None,
        components=[ComponentStatus.model_validate(c) for c in summary["components"]],
        environment_adapters=[
            AdapterStatus.model_validate(a) for a in summary["environment_adapters"]
        ],
        features=FeatureFlags(
            **features,
            memory_access=memory_access,
            replay_fixtures=replay_fixtures,
            replay_fixtures_launchable=launchable_fixtures,
            writes_require_operator_token=True,
            reads_require_token=False,
            per_run_providers=per_run_providers,
            evaluation_modes={m.mode: m.status for m in EVAL_MODES},
        ),
        versions=VersionsView(**summary["versions"], api_schema=api_schema),
    )


@dataclass(frozen=True)
class FixtureCheck:
    """Whether a committed recording can be replayed here and now, and why not."""

    integrity: FixtureIntegrity
    prompt_version_matches: bool
    scenario_available: bool
    reasons: tuple[str, ...]

    @property
    def launchable(self) -> bool:
        return not self.reasons


def fixture_view(info: FixtureInfo, check: FixtureCheck) -> FixtureView:
    return FixtureView(
        id=info.id,
        title=info.title,
        description=info.description,
        scenario=info.scenario,
        strategy=info.strategy,
        use_memory=info.use_memory,
        retrieval=info.retrieval,
        provider=info.provider,
        model=info.model,
        prompt_version=info.prompt_version,
        agents=info.agents,
        approved_by=info.approved_by,
        exported_run=info.exported_run,
        tags=list(info.tags),
        recording_id=info.recording_id,
        file=info.file,
        bytes=info.bytes,
        sha256=info.sha256[:12],
        integrity=check.integrity,
        prompt_version_matches=check.prompt_version_matches,
        scenario_available=check.scenario_available,
        launchable=check.launchable,
        not_launchable_because=list(check.reasons),
        labels=["RECORDED REAL-LLM RUN", "SIMULATED TARGET"],
    )


def replays_view(
    fixtures: Iterable[tuple[FixtureInfo, FixtureCheck]],
    *,
    current_prompt_version: str,
    manifest_error: str | None = None,
) -> ReplaysView:
    return ReplaysView(
        current_prompt_version=current_prompt_version,
        fixtures=[fixture_view(info, check) for info, check in fixtures],
        manifest_error=manifest_error,
    )


def batch_summary(batch: EvalBatch, source: BatchSource) -> BatchSummary:
    """A batch's counts. A success needs the oracle's verified recovery (EvalRecord.success)."""
    records = batch.records
    failures: Counter[str] = Counter(r.failure for r in records if not r.success and r.failure)
    unlabeled = sum(1 for r in records if not r.success and not r.failure)
    if unlabeled:
        failures["uncategorized"] = unlabeled
    return BatchSummary(
        batch_id=batch.batch_id,
        mode=batch.mode,
        title=batch.title,
        created_at=batch.created_at,
        plan=batch.plan,
        providers=list(batch.providers),
        models=list(batch.models),
        prompt_versions=list(batch.prompt_versions),
        n=batch.n,
        outcomes=BatchOutcomes(
            n=batch.n,
            successes=sum(1 for r in records if r.success),
            by_final_state=dict(Counter(r.final_state or "not recorded" for r in records)),
            failures_by_category=dict(failures),
            benchmark_verdicts=(
                dict(Counter(r.benchmark_verdict or "not graded" for r in records))
                if batch.benchmark
                else {}
            ),
        ),
        methods=list(batch.methods),
        limitations=list(batch.limitations),
        source=source,
        targets=list(batch.targets),
        benchmark=batch.benchmark,
    )


def evals_view(batches: Iterable[tuple[EvalBatch, BatchSource]]) -> EvalsView:
    return EvalsView(
        modes=list(EVAL_MODES), batches=[batch_summary(b, source) for b, source in batches]
    )


def batch_view(batch: EvalBatch, source: BatchSource) -> BatchView:
    return BatchView(batch=batch_summary(batch, source), records=list(batch.records))


# ======================================================================================
# Small readers for stored payloads (old or partial traces must never crash a view)
# ======================================================================================


# ======================================================================================
# Published real-cluster runs (evals/published.py)
# ======================================================================================

# What the published files do not hold, in plain words (GET /published `limitations`).
PUBLISHED_LIMITATIONS = (
    "The evidence registry's contents (the tool outputs the agents read) were not exported: "
    "each evidence ID, the tool that produced it and the verifier's verdicts are in the trace, "
    "and what the model was shown is in the run's recording.jsonl.",
    "The approval decision log of the server that ran it was not exported: each request and "
    "decision is in the trace and the run record, as evidence of a past decision only.",
    "Runs that failed or were blocked are published as evaluation records only: their full "
    "traces stay in the owner's run stores, so they have no Trace Explorer view here.",
    "SREGym's grader was run by hand after each graded run: its verdict is the evaluation "
    "record's benchmark_verdict, kept apart from RECAP's oracle, and its output for the passing "
    "run is quoted in evals/live/README.md.",
    "A person approved every fix that ran: these are human-in-the-loop runs, one sample each, "
    "counted and never turned into a success rate.",
)


def published_provenance(run: PublishedRun, folder: str) -> PublishedProvenance:
    """Where a published run comes from, as its checked files and evaluation record say."""
    return PublishedProvenance(
        folder=folder,
        files=sorted(run.entry.files),
        integrity="verified",
        target=run.entry.target,
        title=run.entry.title,
        notes=list(run.entry.notes),
        recording_id=run.recording_id,
        batch_id=run.batch.batch_id,
        benchmark=run.batch.benchmark,
        benchmark_verdict=run.eval_record.benchmark_verdict,
        approvals=list(run.eval_record.approvals),
    )


def published_view(index: PublishedIndex, summaries: Sequence[RunSummary]) -> PublishedView:
    """GET /published: the runs that loaded, the records without a trace, the refusals."""
    batches = {run.batch.batch_id: run.batch for run in index.runs}
    batches.update({item.batch.batch_id: item.batch for item in index.records_without_trace})
    return PublishedView(
        manifest_schema=index.schema_version,
        runs=list(summaries),
        records_without_trace=[
            RecordWithoutTraceView(
                run_id=item.record.run_id,
                batch_id=item.batch.batch_id,
                target=item.record.target,
                scenario_id=item.record.scenario_id,
                started_at=item.record.started_at,
                final_state=item.record.final_state,
                oracle_verified=item.record.oracle_verified,
                failure=item.record.failure,
                failure_detail=item.record.failure_detail,
                approvals=list(item.record.approvals),
                benchmark_verdict=item.record.benchmark_verdict,
                model=item.record.model,
                provider=item.record.provider,
            )
            for item in index.records_without_trace
        ],
        rejected=[
            RejectedArtifactView(folder=r.folder, run_id=r.run_id, reason=r.reason)
            for r in index.rejected
        ],
        manifest_error=index.manifest_error,
        benchmarks={batch_id: b.benchmark for batch_id, b in batches.items() if b.benchmark},
        limitations=list(PUBLISHED_LIMITATIONS),
    )


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _strs(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _dicts(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _signals(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): float(v) for k, v in value.items() if _float(v) is not None}


def _origin(value: Any) -> Any:
    known = ("scripted", "local_llm", "free_hosted_llm", "paid_llm", "replay")
    return value if value in known else None


def _sum_reported(values: Sequence[int | None]) -> int | None:
    reported = [v for v in values if v is not None]
    return sum(reported) if reported else None


def _ms(delta: Any) -> float:
    return round(delta.total_seconds() * 1000, 1)


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None
