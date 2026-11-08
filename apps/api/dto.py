"""Typed response contracts of the RECAP HTTP API: schema "recap-api-v1" (ADR 0027).

Where it sits:

    RunRecord, Evidence, TraceEvent  (what a run persisted; recap/core/models.py)
        --> apps/api/views.py        (builds these models from them, and nothing else)
        --> apps/api/main.py         (each endpoint declares one as its response_model,
                                      so GET /openapi.json documents every field)

Three rules hold for every model here:

  * Every value is copied or counted from what a run recorded (its checkpoint, its stored
    trace events, its evidence, the oracle's verdict) or from server configuration. Nothing
    is invented to fill a gap: a field the record lacks is null and its name is listed in
    `not_recorded`, so a screen can say "Not recorded" instead of showing a made-up value.
  * Text that a model, a tool, an alert or a runbook wrote stays data. Fields that can hold
    such text say so (`author`, `untrusted_text`, `trust_class`); RECAP's own rule text is
    marked as RECAP's.
  * No response carries a key, a token, a signed capability or a local file path.

Versioning: every response has the header X-RECAP-API-Schema: recap-api-v1, and every
top-level JSON object defined here also has `schema_version`. The two list endpoints
(GET /runs and GET /providers) keep the JSON arrays the dashboard has always read, so they
carry the version in the header only (and GET /runs the number of matching runs in
X-Total-Count). A change that renames or removes a field needs a new schema name; adding a
field does not.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evals.records import EvalMode, EvalRecord
from recap.core.models import (
    Disagreement,
    EnvironmentInfo,
    Evidence,
    FailureMechanism,
    IncidentState,
    ProviderOrigin,
    RunRecord,
    SourceType,
    StrategyMode,
    TrustLevel,
)

ApiSchema = Literal["recap-api-v1"]
API_SCHEMA: ApiSchema = "recap-api-v1"
SCHEMA_HEADER = "X-RECAP-API-Schema"  # on every response
TOTAL_HEADER = "X-Total-Count"  # GET /runs: how many runs match the filters

# Where a run's decisions came from (RunRecord.provider_info.origin): the run-origin badge.
ProviderClass = ProviderOrigin

# Where a run stands as far as THIS server process can tell (RunManager.status).
# 'published' (M6): a real-cluster run published with the repository (evals/live/MANIFEST.json),
# read from its checked files: historical evidence, never running, never acted on.
RunStatus = Literal["running", "finished", "error", "external", "unknown", "published"]

# How a run ended, in one word, from its recorded state, actions and note (views.run_outcome).
RunOutcome = Literal[
    "in_progress",  # not in a terminal state yet
    "resolved",  # the independent oracle verified the recovery
    "failed",  # FAILED: the evidence stayed insufficient, or a person must decide
    "error",  # FAILED with an error (a provider failure, a crash): see `error`
    "cancelled",  # FAILED because someone cancelled the run
    "approval_rejected",  # BLOCKED: the person said no
    "approval_expired",  # BLOCKED: nobody decided in time
    "safety_blocked",  # BLOCKED: the policy or the evidence gate refused the fix
    "budget_exhausted",  # a run limit stopped it safely
    "quota_deferred",  # a free model's quota ran out: deferred, nothing decided
]

# The status of one implementation behind an interface (GET /system).
ComponentState = Literal[
    "active",  # wired in: runs use it now
    "available",  # implemented and usable when a run asks for it
    "unavailable",  # implemented, but cannot run now (the detail says why)
    "disabled",  # implemented and switched off by configuration
    "planned",  # not implemented in this repository (a later milestone)
    "error",  # configured, but it cannot be built (the detail says why)
]

# How far a piece of evidence can be trusted, by where its text came from.
TrustClass = Literal[
    "untrusted_tool_output",  # an MCP tool's answer: data that might lie or inject
    "untrusted_runbook_text",  # a runbook section (also the receipt of an agent's search)
    "untrusted_memory_text",  # what a past episode recorded
    "untrusted_alert_text",  # the alert's own words
    "recap_trusted_text",  # produced by RECAP's own code
]

# Whether a committed recording's bytes match the SHA-256 its manifest entry pins.
FixtureIntegrity = Literal["intact", "altered", "missing"]

# Where an evaluation batch is kept: in git, or in this machine's data folder.
BatchSource = Literal["published", "local"]

# Who wrote a piece of text the API returns.
TextAuthor = Literal["model", "replayed_model", "scripted_policy", "recap", "unknown"]

TimelineKind = Literal[
    "lifecycle",
    "tool_discovery",
    "retrieval",
    "memory",
    "routing",
    "round_start",
    "agent_start",
    "context",
    "model_request",
    "agent_decision",
    "policy",
    "tool_call",
    "tool_result",
    "injection_flag",
    "agent_finish",
    "round_summary",
    "verification",
    "proposal",
    "approval_requested",
    "approval_decided",
    "write_started",
    "write_executed",
    "oracle",
    "budget",
    "error",
    "summary",
    "other",
]


class ApiModel(BaseModel):
    """Base of every response model: an unknown field is an error, so a typo in a view
    builder fails a test instead of reaching a client."""

    model_config = ConfigDict(extra="forbid")


class Versioned(ApiModel):
    """A top-level JSON object: it names the API schema it follows."""

    schema_version: ApiSchema = API_SCHEMA


# ======================================================================================
# Runs: GET /runs, GET /runs/{id}
# ======================================================================================


class OracleVerdict(ApiModel):
    """The independent recovery oracle's verdict, exactly as the run record holds it."""

    oracle: str
    verified: bool
    healthy_before: bool
    healthy_after: bool
    checked_at: datetime
    services_checked: list[str] = Field(default_factory=list)


class ReplayProvenance(ApiModel):
    """Which recording a replayed run answered from, and where the original run is."""

    fixture_id: str | None  # the committed recording the client chose (RunConfig.replay_fixture)
    recording_id: str | None  # the recording actually replayed (its header)
    # Whether the replayed bytes are the chosen fixture's (None: no fixture was named). False
    # would mean the run replayed another file.
    recording_matches_fixture: bool | None
    # The replayed bytes are a committed fixture: fixtures/recordings/MANIFEST.json pins
    # their SHA-256 (for records older than M4: the manifest lists their recording id).
    committed_fixture: bool
    original_provider: str | None
    model: str | None
    prompt_version: str | None  # the prompts the ORIGINAL run was recorded with
    recorded_at: str | None
    fixture_title: str | None
    fixture_sha256: str | None  # first 12 hex characters of the replayed bytes' SHA-256
    original_export: str | None  # repo-relative folder of the original run's export
    originally_approved_by: str | None  # who approved the original fix (the replay asks again)


class ExportProvenance(ApiModel):
    """A shareable export of this very run (scripts/export_run_trace.py), committed in git."""

    folder: str  # repo-relative, e.g. "evals/live/2026-10-25-..."
    files: list[str]


class PublishedProvenance(ApiModel):
    """Where a published run comes from (evals/published.py): an allowlisted folder of the
    repository whose files matched their pinned SHA-256 and size when this server read them,
    and the run's evaluation record. Historical evidence: nothing of it can be acted on."""

    folder: str  # repo-relative, e.g. "evals/live/2026-11-07-..."
    files: list[str]
    integrity: Literal["verified"]  # every pinned file matched; a run that does not is never shown
    target: str  # the real cluster it worked on: "kind" or "sregym"
    title: str  # a person's, from the manifest (checked in review)
    notes: list[str]  # a person's, from the manifest
    recording_id: str  # the run's own model recording (recording.jsonl in the folder)
    batch_id: str  # the evaluation batch holding its record
    benchmark: str | None  # the grader behind benchmark_verdict (the batch's), if any
    benchmark_verdict: Literal["pass", "fail"] | None  # the benchmark's own grader; never RECAP's
    approvals: list[str]  # the evaluation record's account of each decision (a past decision)


class RunSummary(ApiModel):
    """One run in a few dozen fields: identity, provenance, outcome and oracle verdict.

    The first twelve fields are the ones GET /runs has always returned (the dashboard reads
    them); the rest were added in M4.
    """

    run_id: str
    scenario_id: str
    state: IncidentState
    label: str
    created_at: datetime
    updated_at: datetime
    modes: list[StrategyMode]  # each investigation round's routing: single or parallel
    recovered: bool  # the oracle verified the recovery
    outcome_note: str  # the controller's own sentence
    provider: str  # the provider's id, e.g. "ollama:qwen3:8b" or "deterministic-scripted-policy"
    tool_calls: int
    pending_approval: bool

    status: RunStatus
    terminal: bool
    outcome: RunOutcome
    provider_class: ProviderClass | None
    provider_kind: Literal["scripted", "llm", "replay"] | None
    model: str | None  # None for the scripted policy, which is not a model
    # The prompt version the run's provider rendered. None for the scripted policy, which
    # uses no prompts (its record's versions.prompt_version is only a historical default).
    prompt_version: str | None
    environment_kind: str | None
    environment_simulated: bool | None
    config_hash: str | None
    code_version: str | None
    recap_version: str | None
    started_at: datetime
    ended_at: datetime | None  # when the run reached its terminal state; None while it runs
    wall_seconds: float | None  # ended - started, including any wait for a human
    rounds: int
    agents: list[str]  # every agent role that investigated
    model_requests: int  # HTTP requests sent to a model IN THIS RUN (a replay sends none)
    oracle: OracleVerdict | None  # None: no fix reached the oracle
    replay: ReplayProvenance | None  # only for replayed runs
    export: ExportProvenance | None  # only when this run was exported
    recording: str | None  # the id of this run's own model recording (live model runs)
    episode_id: str | None
    error: str | None
    labels: list[str]  # e.g. "SIMULATED ENVIRONMENT", "REPLAY", "SCRIPTED BASELINE (NOT AN LLM)"
    not_recorded: list[str] = Field(default_factory=list)
    published: PublishedProvenance | None = None  # only for a published run (status "published")


class RunDetail(Versioned):
    """GET /runs/{id}: the summary, plus the stored record and evidence as before.

    `status`, `record`, `evidence` and `error` keep the shape the dashboard has always read.
    `record` is null while a run this server just started has not saved its first checkpoint.
    """

    status: RunStatus
    summary: RunSummary | None
    record: RunRecord | None
    evidence: list[Evidence]
    error: str | None


class RunStarted(Versioned):
    """POST /incidents/run (202): the new run's id and what it will run with."""

    run_id: str
    status: Literal["started"] = "started"
    scenario_id: str
    provider: str  # the configured provider name the run uses
    provider_class: ProviderClass
    replay_fixture: str | None
    labels: list[str]


# ======================================================================================
# Timeline: GET /runs/{id}/timeline
# ======================================================================================


class ModelRequest(ApiModel):
    """One HTTP request a provider sent to a model (an `llm.call` event)."""

    provider: str
    model: str
    model_reported: str | None
    origin: ProviderClass
    purpose: str  # investigate or propose
    mode: str  # json or tools
    attempt: int
    retry: bool  # attempt > 1: a corrective retry after an unusable answer
    status: int | None
    ok: bool
    response_kind: str | None
    input_tokens: int | None  # as the server reported them; None when it reported none
    output_tokens: int | None
    duration_ms: float  # from sending the request to its answer, as RECAP measured it
    # The server's own time for the request, when it reports one. Ollama's (total_duration)
    # counts from the moment it received the request, so a wait in its own queue behind
    # another agent's request is part of it.
    server_ms: float | None
    # duration_ms - server_ms: time outside the model server (connection and transfer).
    # It is NOT a wait in the server's queue: that wait is inside server_ms.
    outside_server_ms: float | None
    # The server's compute (load + prompt evaluation + generation), when it reports it, and
    # server_ms minus it: the time the request waited inside the server (mostly queued
    # behind another agent's request on a server that answers one at a time).
    server_compute_ms: float | None = None
    server_wait_ms: float | None = None
    replayed: bool  # answered from a recording; its times are the ORIGINAL run's
    finish_reason: str | None


class TimelineEntry(ApiModel):
    """One stored trace event, with the fields a timeline filters and draws by.

    `payload` is the event as stored (secrets were redacted before it was written; local
    folders are removed from the response). When `untrusted_text` is true it holds text a
    model, a tool or an alert wrote: show it as quoted data, never as RECAP's own words.
    """

    seq: int
    ts: datetime
    stage: str
    category: str
    kind: TimelineKind
    agent: str | None
    round: int | None  # the investigation round in progress (None outside INVESTIGATING)
    step: int | None
    tool: str | None
    invocation_id: str | None
    verdict: str | None  # policy verdict, approval decision or verification status
    ok: bool | None
    evidence_ids: list[str] = Field(default_factory=list)
    duration_ms: float | None
    model_request: ModelRequest | None = None
    untrusted_text: bool
    payload: dict[str, Any]


class AgentLane(ApiModel):
    """One agent's turn in one round: its own lane on a timeline."""

    agent: str
    round: int
    mode: str | None  # how the round was routed: single or parallel
    started_at: datetime
    finished_at: datetime | None  # None: the turn has not finished (or never did)
    duration_ms: float | None
    outcome: str  # hypothesis, need_more_evidence, step_limit, error, ..., or running
    partial: bool  # kept from a round a budget stop cut short; never verified
    error: str | None
    steps: int | None
    tool_calls: int | None
    model_requests: int | None
    input_tokens: int | None
    output_tokens: int | None
    model_request_ms: float | None  # summed time of this lane's model requests
    model_server_ms: float | None  # summed server time, over the requests that report it
    evidence_ids: list[str] = Field(default_factory=list)
    hypotheses: int | None


class RoundInfo(ApiModel):
    """One investigation round: how it was routed, who ran, and whether turns overlapped."""

    round: int
    mode: str
    rule_id: str
    reason: str  # the router's own words
    agents: list[str]
    focus_services: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)  # RECAP's text (verifier)
    leads: list[str] = Field(default_factory=list)  # RECAP's text (verifier)
    escalation: bool | None
    wall_ms: float | None  # first agent start to last agent finish
    sum_ms: float | None  # the agents' turn times added up
    # > 0 only when turns ran at the same time. Overlapping turns are not overlapping
    # inference: a local Ollama server may answer one request at a time.
    overlap_ms: float | None
    model_request_ms: float | None  # summed time of the round's model requests
    verification_status: str | None  # what the verifier concluded after the round


class RetryCounts(ApiModel):
    model_retries: int  # corrective retries (attempt > 1)
    failed_model_requests: int  # requests that gave no usable answer (an error status or unusable)
    fix_attempts: int  # writes sent to the target (mitigation.executed)


class TimelineFilters(ApiModel):
    agent: str | None = None
    round: int | None = None
    kind: TimelineKind | None = None
    tool: str | None = None


class TimelineView(Versioned):
    """GET /runs/{id}/timeline: one page of the run's trace, with lanes and rounds.

    Page with `after_seq` (resume after the last seq you have) and `limit`; `next_after_seq`
    is the cursor of the next page. Filters apply before paging.
    """

    run_id: str
    status: RunStatus
    state: IncidentState | None
    live: bool  # this server is running it: poll again for more events
    trace_available: bool  # False: no stored event (a missing or deleted trace)
    total_events: int
    after_seq: int
    limit: int
    filters: TimelineFilters
    matching_events: int  # events that pass the filters, over the whole trace
    next_after_seq: int | None
    has_more: bool
    entries: list[TimelineEntry]
    lanes: list[AgentLane]
    rounds: list[RoundInfo]
    counts: dict[str, int]  # events per kind over the whole trace
    retries: RetryCounts
    notes: list[str] = Field(default_factory=list)
    not_recorded: list[str] = Field(default_factory=list)


# ======================================================================================
# Evidence and decisions: GET /runs/{id}/evidence
# ======================================================================================


class EvidenceItem(ApiModel):
    """One immutable observation in the run's evidence registry."""

    id: str
    source_type: SourceType
    source_ref: str  # e.g. "mcp:get_service_logs(service=checkout)"
    tool: str | None
    timestamp: datetime
    recorded_by: str | None  # the agent whose call produced it
    round: int | None  # the investigation round its call was made in (from the trace)
    trust_class: TrustClass
    trust_level: TrustLevel
    flags: list[str] = Field(default_factory=list)  # e.g. prompt_injection_suspected, truncated
    token_estimate: int
    content: str  # the bounded text an LLM would read (untrusted)
    supports: list[str] = Field(default_factory=list)  # claim ids citing it as support
    contradicts: list[str] = Field(default_factory=list)  # claim ids citing it against them
    in_rule_basis: bool  # a passing evidence rule relied on it


class ClaimView(ApiModel):
    """One hypothesis an agent made, with the evidence it cited."""

    claim_id: str  # "r{round}.{agent}.{n}", built by the API to link evidence and claims
    round: int
    agent: str
    author: TextAuthor  # who wrote `claim` and `confidence_note`
    claim: str
    mechanism: FailureMechanism
    target_service: str | None
    details: dict[str, Any] = Field(default_factory=dict)
    origin: str  # investigation, or memory
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    unknown_evidence_ids: list[str] = Field(default_factory=list)  # cited, not in this run
    confidence_note: str
    partial: bool


class FindingView(ApiModel):
    """How one agent's turn in one round ended."""

    agent: str
    round: int
    outcome: str
    partial: bool
    error: str | None
    author: TextAuthor  # who wrote `summary`
    summary: str
    evidence_ids: list[str] = Field(default_factory=list)
    doc_refs: list[str] = Field(default_factory=list)  # runbook sections it saw
    claims: list[ClaimView] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime


class CheckView(ApiModel):
    """The verifier's judgment of one (merged) claim. Rule lines are RECAP's own text."""

    claim: str  # the claim's text as an agent wrote it (untrusted)
    mechanism: str
    target: str | None
    status: str  # supported, contradicted, insufficient
    proposers: list[str] = Field(default_factory=list)
    basis_evidence_ids: list[str] = Field(default_factory=list)  # what the passing rules used
    detail: str = ""  # the release or dependency that told this claim apart
    passed: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    # The final report also keeps the claim's own citations (the trace does not).
    supporting_evidence_ids: list[str] | None = None
    contradicting_evidence_ids: list[str] | None = None


class BestClaim(ApiModel):
    claim: str
    mechanism: str
    target_service: str | None


class VerificationView(ApiModel):
    """One verification of the agents' claims (after a round), as recorded."""

    source: Literal["trace", "run_record"]
    seq: int | None  # the trace event it comes from
    round: int | None
    status: str
    summary: str  # RECAP's text
    best: BestClaim | None
    checks: list[CheckView] = Field(default_factory=list)
    unknown_evidence_ids: list[str] = Field(default_factory=list)
    focus_services: list[str] = Field(default_factory=list)
    disagreements: list[Disagreement] = Field(default_factory=list)
    leads: list[str] = Field(default_factory=list)


class ProposalView(ApiModel):
    """A fix a provider drafted. `rationale` is the provider's text; the digest binds the
    approval to exactly this action, target and parameters."""

    proposal_id: str
    action: str
    target: str
    params: dict[str, Any] = Field(default_factory=dict)
    digest: str
    author: TextAuthor
    source: str  # runbook, memory, fallback, llm, redteam
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    declared_risk: str  # what the proposer claimed (never trusted)


class ProposalCheckView(ApiModel):
    """The evidence gate's check of a proposal (rules VP1 to VP4)."""

    seq: int
    proposal_id: str
    ok: bool
    problems: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    enforced: bool


class PolicyVerdictView(ApiModel):
    """A safety-policy decision on a proposed fix (the gateway's, from trusted config)."""

    tool: str
    verdict: str
    rule_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    risk: str | None
    caller: str | None
    decided_at: datetime


class InjectionFlagView(ApiModel):
    seq: int
    evidence_id: str
    tool: str | None
    agent: str | None
    patterns: list[str] = Field(default_factory=list)


class EvidenceView(Versioned):
    """GET /runs/{id}/evidence: what was observed, what was claimed, and how it was judged."""

    run_id: str
    status: RunStatus
    state: IncidentState | None
    trace_available: bool
    registry: list[EvidenceItem]
    findings: list[FindingView]
    verifications: list[VerificationView]
    final_report: VerificationView | None
    proposals: list[ProposalView]
    proposal_checks: list[ProposalCheckView]
    policy_verdicts: list[PolicyVerdictView]  # on proposed fixes only (reads are in the timeline)
    injection_flags: list[InjectionFlagView]
    unknown_cited_ids: list[str]  # cited by a claim or proposal, but not in this run
    trust_classes: dict[str, str]  # what each trust class means
    not_recorded: list[str] = Field(default_factory=list)


# ======================================================================================
# Retrieval and memory: GET /runs/{id}/retrieval
# ======================================================================================


class SearchResultView(ApiModel):
    chunk_id: str
    doc_path: str | None
    heading: str | None
    rank: int | None
    score: float | None
    retriever: str | None
    signals: dict[str, float] = Field(default_factory=dict)  # only rankings the search used
    cited_by_agents: list[str] = Field(default_factory=list)  # agents whose context held it
    cited_by_proposal: bool = False


class EmbeddingCallView(ApiModel):
    """The embedding request behind a hybrid search (as the embedder recorded it)."""

    model: str | None
    inputs: int | None
    status: int | None
    input_tokens: int | None
    duration_ms: float | None
    replayed: bool | None


class SearchView(ApiModel):
    """One runbook search. Its duration is not recorded (only a hybrid search's embedding
    request is timed)."""

    seq: int
    ts: datetime
    stage: str  # initial (the alert), proposal (agentic RAG), agent (search_runbooks)
    reason: str
    query: str
    query_author: TextAuthor  # recap built it, or an agent (a model or the scripted policy)
    agent: str | None
    mode: str | None  # bm25 or hybrid
    retriever: str | None  # what this search ranked with
    embedding: EmbeddingCallView | None
    results: list[SearchResultView] = Field(default_factory=list)


class SkipView(ApiModel):
    seq: int
    stage: str | None
    reason: str


class RefusedSearchView(ApiModel):
    seq: int
    agent: str | None
    error: str | None


class MemoryHitView(ApiModel):
    episode_id: str
    similarity: float | None
    signals: dict[str, float] = Field(default_factory=dict)  # lexical, semantic
    outcome: str | None
    verified: bool | None
    mechanism: str | None
    service: str | None
    partition: str | None
    safe_to_reuse: bool | None  # None: not assessed against live evidence (yet)
    mechanism_match: bool | None
    reason: str | None  # RECAP's reuse verdict, in its own words
    supporting: list[str] = Field(default_factory=list)
    contradicting: list[str] = Field(default_factory=list)


class MemorySearchView(ApiModel):
    seq: int
    readable_partitions: list[str] = Field(default_factory=list)
    created_before: str | None  # a replay reads memory as of its recording's start
    semantic: str | None  # the embedder of a semantic search, if any
    hits: list[MemoryHitView] = Field(default_factory=list)


class MemoryAssessmentView(ApiModel):
    seq: int
    round: int | None
    hits: list[MemoryHitView] = Field(default_factory=list)
    restore_points: Any = None


class MemoryWriteView(ApiModel):
    episode_id: str
    partition: str | None
    outcome: str | None
    verified: bool | None


class MemoryRetrievalView(ApiModel):
    enabled: bool
    read_partitions: list[str] = Field(default_factory=list)
    write_partition: str | None
    write_enabled: bool
    searches: list[MemorySearchView] = Field(default_factory=list)
    assessments: list[MemoryAssessmentView] = Field(default_factory=list)
    final_hits: list[MemoryHitView] = Field(default_factory=list)  # as the run ended
    written: MemoryWriteView | None
    skipped: list[str] = Field(default_factory=list)  # why nothing was written, as recorded


class RetrievalView(Versioned):
    """GET /runs/{id}/retrieval: runbook searches, citations and memory, as recorded."""

    run_id: str
    status: RunStatus
    state: IncidentState | None
    trace_available: bool
    use_rag: bool
    mode: str | None
    embedder: str | None  # the run's query embedder (hybrid runs only)
    embedding_server: dict[str, Any] | None  # what the embedding server reported
    embedding_requests: int | None
    embedding_input_tokens: int | None
    replayed_embedding_requests: int | None
    agent_searches: int | None
    agent_search_refusals: list[RefusedSearchView]
    knowledge_overlays: list[str]  # extra knowledge folders (e.g. a red-team runbook)
    searches: list[SearchView]
    skipped: list[SkipView]
    context_docs: list[SearchResultView]  # the runbook sections in context at the end
    memory: MemoryRetrievalView
    not_recorded: list[str] = Field(default_factory=list)


# ======================================================================================
# Usage: GET /runs/{id}/usage
# ======================================================================================


class ProviderLimitsView(ApiModel):
    """A provider's configured limits (configs/providers.yaml). Never a key."""

    max_output_tokens: int | None = None
    context_tokens: int | None = None
    request_timeout_seconds: float | None = None
    max_attempts: int | None = None
    run_budget_overrides: dict[str, Any] = Field(default_factory=dict)
    # Free hosted plans only: the published free limits and RECAP's own lower caps.
    free_daily_request_limit: dict[str, int] | None = None
    limits_checked_on: str | None = None
    caps: dict[str, int] | None = None
    reserved_output_tokens: int | None = None


class ModelUsageView(ApiModel):
    """Every request a run sent to one model, added up from its `llm.call` events."""

    provider: str
    model: str
    provider_class: ProviderClass
    requests: int
    ok_requests: int
    failed_requests: int
    retries: int
    replayed_requests: int
    input_tokens: int | None  # summed server counts; None when no request reported any
    output_tokens: int | None
    requests_without_token_counts: int
    request_ms: float  # summed request time, as RECAP measured it
    # Summed server time, over the requests that report one (a wait in the server's own
    # queue is part of it, see ModelRequest.server_ms).
    server_ms: float | None
    requests_with_server_ms: int
    # Request time minus server time, over requests that have both: connection and
    # transfer, not queueing.
    outside_server_ms: float | None
    # Summed server compute, and summed waits inside the server, over the requests that
    # report both server time and compute.
    server_compute_ms: float | None = None
    server_wait_ms: float | None = None
    by_purpose: dict[str, int] = Field(default_factory=dict)
    by_agent: dict[str, int] = Field(default_factory=dict)
    limits: ProviderLimitsView | None  # the CURRENT configuration of that provider


class EstimatedTokens(ApiModel):
    """RECAP's own estimate, never a model's count."""

    tokens: int
    estimate: Literal[True] = True
    method: str = (
        "RECAP's estimate of the context packs it built for providers (about 4 characters "
        "per token); not a token count reported by any model"
    )


class UsageTotals(ApiModel):
    """The run record's own totals (the budget tracker's counts)."""

    model_requests: int
    input_tokens: int | None
    output_tokens: int | None
    unreported_requests: int
    tool_calls: int
    steps: int


class BudgetView(ApiModel):
    limits: dict[str, Any] | None  # the run's RunBudget
    used: dict[str, Any] | None  # the budget snapshot in the run summary event


class EmbeddingUsageView(ApiModel):
    requests: int
    input_tokens: int | None
    replayed: int


class UsageView(Versioned):
    """GET /runs/{id}/usage: requests, reported tokens, estimates and time, per model.

    No dollar figure except the provider's own cost label (this project's runs cost $0).
    """

    run_id: str
    status: RunStatus
    state: IncidentState | None
    trace_available: bool
    provider_class: ProviderClass | None
    models: list[ModelUsageView]
    totals: UsageTotals
    estimated_context_tokens: EstimatedTokens
    budget: BudgetView
    embedding: EmbeddingUsageView | None
    cost_label: str | None  # the controller's one line about cost, from the run summary
    notes: list[str] = Field(default_factory=list)
    not_recorded: list[str] = Field(default_factory=list)


# ======================================================================================
# Providers: GET /providers (a JSON array of ProviderView)
# ======================================================================================


class ProviderView(ApiModel):
    """One configured decision provider and whether it could run now.

    The first block holds the catalog's fields as GET /providers has always returned them
    (recap/providers/catalog.py); a key is only ever reported as present or not.
    """

    name: str
    kind: Literal["scripted", "llm", "replay"]
    origin: ProviderClass
    model: str | None
    mode: str | None
    description: str
    default: bool
    status: str  # ready, needs_setup, stopped, disabled, unavailable
    detail: str
    usage: dict[str, int] | None = None
    base_url: str | None = None
    think: bool | None = None
    context_tokens: int | None = None
    installed: list[str] | None = None
    key_env: str | None = None  # the NAME of the variable that holds the key
    key_present: bool | None = None  # whether it is set; its value is never read here
    opt_in_env: str | None = None
    opted_in: bool | None = None
    attestation_env: str | None = None
    confirmed_on: str | None = None
    confirmation_age_days: int | None = None
    confirmation_fresh: bool | None = None
    allowed_models: list[str] | None = None
    free_daily_request_limit: dict[str, int] | None = None
    limits_checked_on: str | None = None
    caps: dict[str, int] | None = None
    last_rate_limit: dict[str, Any] | None = None
    stopped: dict[str, Any] | None = None
    recording: str | None = None
    replay_of: dict[str, Any] | None = None

    provider_class: ProviderClass
    key_required: bool
    selectable: bool  # may a client pick it for one run (POST /incidents/run) now?
    selection_rule: str  # why, in plain words
    limits: ProviderLimitsView
    limitations: list[str] = Field(default_factory=list)


# ======================================================================================
# System: GET /system
# ======================================================================================


class ComponentStatus(ApiModel):
    """Which implementation sits behind one interface, and whether it is really in use."""

    id: str
    interface: str
    implementation: str
    status: ComponentState
    detail: str = ""


class AdapterStatus(ApiModel):
    """One environment adapter. Only an implemented adapter can ever be operational."""

    kind: str
    adapter: str | None  # the class, when one exists in this repository
    configured: bool  # configs/environment.yaml names this kind
    status: ComponentState
    operational: bool  # can it run an incident on this server (only the configured adapter)?
    milestone: str | None  # when a planned adapter is due (M5)
    detail: str


class MemoryAccessView(ApiModel):
    read: list[str]
    write: str | None  # None: this kind of run never writes memory


class FeatureFlags(ApiModel):
    retrieval_default: str
    hybrid_ready: bool
    embeddings_enabled: bool  # RECAP_ENABLE_EMBEDDINGS: hybrid by default
    vector_index: dict[str, Any] | None  # identity of the stored runbook vectors
    knowledge_chunks: int
    knowledge_overlays: list[str]
    agent_search: dict[str, int]
    memory_partitions: list[str]
    memory_access: dict[str, MemoryAccessView]  # by provider class
    replay_fixtures: int
    replay_fixtures_launchable: int
    writes_require_operator_token: bool
    reads_require_token: bool
    per_run_providers: list[str]  # providers a client may pick for one run now
    otel: bool
    evaluation_modes: dict[str, str]  # mode -> implemented / planned


class VersionsView(ApiModel):
    recap: str
    api_schema: str
    trace_schema: str
    prompt_version: str  # the prompts real models are asked with
    policy_version: str
    router: str
    verifier: str
    config_hash: str
    fixture_hash: str
    code_version: str
    python: str


class SystemView(Versioned):
    """GET /system: what is plugged in where, and what is only planned.

    The first block holds the fields GET /system has always returned (the dashboard reads
    them); `components`, `environment_adapters`, `features` and `versions` were added in M4.
    """

    environment: EnvironmentInfo
    environment_kind: str
    tool_transport: str
    provider: dict[str, Any]
    providers_available: list[dict[str, Any]]
    retriever: str
    retrieval_mode: str
    hybrid_ready: bool
    embedder: str | None
    knowledge_chunks: int
    router: str
    safety_policy: str
    verifier: str
    oracle: str
    memory_episodes: int
    trace_sinks: list[str]
    config_hash: str
    fixture_hash: str
    code_version: str

    label: str  # e.g. "SIMULATED LOCAL ENVIRONMENT · V1"
    simulation_notice: str | None
    components: list[ComponentStatus]
    environment_adapters: list[AdapterStatus]
    features: FeatureFlags
    versions: VersionsView


# ======================================================================================
# Replays: GET /replays
# ======================================================================================


class FixtureView(ApiModel):
    """One committed recording of a real model run (fixtures/recordings/MANIFEST.json)."""

    id: str
    title: str
    description: str
    scenario: str
    strategy: str
    use_memory: bool
    retrieval: str
    provider: str
    model: str
    prompt_version: str
    agents: str
    approved_by: str
    exported_run: str | None
    tags: list[str] = Field(default_factory=list)
    recording_id: str
    file: str
    bytes: int
    sha256: str  # first 12 hex characters of the pinned hash
    integrity: FixtureIntegrity  # the file's bytes against the manifest
    prompt_version_matches: bool  # recorded with the prompts this server renders
    scenario_available: bool  # this server's environment has the scenario
    launchable: bool
    not_launchable_because: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class ReplaysView(Versioned):
    current_prompt_version: str
    fixtures: list[FixtureView]
    manifest_error: str | None = None
    how_to_launch: str = (
        "POST /incidents/run (operator) with {scenario_id: <the fixture's scenario>, config: "
        "{provider: 'replay', replay_fixture: <id>}}; routing, memory and retrieval come "
        "from the fixture. The replay calls no model and asks the operator to approve again."
    )


# ======================================================================================
# Evaluations: GET /evals, GET /evals/{batch_id}
# ======================================================================================


class EnvironmentCheckItem(ApiModel):
    name: str
    ok: bool
    detail: str


class EnvironmentCheck(ApiModel):
    """POST /environment/check: can the configured target be reached right now? A real
    cluster runs its adapter's own preflight (pinned credentials, API server, versions,
    clock); nothing is changed and no run starts."""

    checked_at: datetime
    environment_kind: str
    simulated: bool
    ok: bool
    checks: list[EnvironmentCheckItem] = Field(default_factory=list)


class EvalModeInfo(ApiModel):
    mode: EvalMode
    status: Literal["implemented", "planned"]
    description: str


class BatchOutcomes(ApiModel):
    """Counts over a batch's records. A success needs the oracle's verified recovery."""

    n: int
    successes: int
    by_final_state: dict[str, int] = Field(default_factory=dict)
    failures_by_category: dict[str, int] = Field(default_factory=dict)
    # A benchmark grader's own verdicts ("pass", "fail", "not graded"), when the batch names
    # one (BatchSummary.benchmark): the benchmark's numbers, never added to successes.
    benchmark_verdicts: dict[str, int] = Field(default_factory=dict)


class BatchSummary(ApiModel):
    batch_id: str
    mode: EvalMode
    title: str
    created_at: datetime
    plan: str | None
    providers: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    n: int
    outcomes: BatchOutcomes
    methods: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    source: BatchSource  # in git, or this machine's data folder
    targets: list[str] = Field(default_factory=list)  # "simulated", or a real cluster's kind
    benchmark: str | None = None  # the grader behind outcomes.benchmark_verdicts


class EvalsView(Versioned):
    modes: list[EvalModeInfo]
    batches: list[BatchSummary]
    legacy_report: str = "/evals/results"  # the scripted benchmark report, as before


class BatchView(Versioned):
    batch: BatchSummary
    records: list[EvalRecord]


# ======================================================================================
# GET /published: the real-cluster runs published with the repository (M6)
# ======================================================================================


class RecordWithoutTraceView(ApiModel):
    """A real-cluster run known only from its evaluation record: its full trace was not
    published, so it has no Trace Explorer view. Every field is the record's own."""

    run_id: str
    batch_id: str
    target: str
    scenario_id: str
    started_at: datetime | None
    final_state: str
    oracle_verified: bool
    failure: str  # the evaluation's failure category ("" for a verified success)
    failure_detail: str
    approvals: list[str]
    benchmark_verdict: Literal["pass", "fail"] | None
    model: str | None
    provider: str


class RejectedArtifactView(ApiModel):
    """A manifest entry that failed a check: nothing of it is shown, only why."""

    folder: str | None
    run_id: str | None
    reason: str


class PublishedView(Versioned):
    """GET /published: every published real-cluster run that passed its checks (as run
    summaries with their provenance), the real-cluster evaluation records without a published
    trace (failures included), and any entry that was refused, with the reason."""

    manifest_schema: str
    runs: list[RunSummary]
    records_without_trace: list[RecordWithoutTraceView]
    rejected: list[RejectedArtifactView]
    manifest_error: str | None
    benchmarks: dict[str, str]  # batch id -> the grader behind its records' benchmark_verdict
    limitations: list[str]  # what the published files do not hold, in plain words
