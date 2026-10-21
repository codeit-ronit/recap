"""Shared, typed contracts used by every RECAP layer.

Every piece of data that crosses a module boundary is a Pydantic model defined
here, so all layers agree on one shape and JSON serialization stays stable.
Read this file first when learning the codebase: it is RECAP's vocabulary.

Sections:
  1. Enums             - states, roles, risk tiers, outcomes
  2. Incident & budget - what enters the controller
  3. Evidence          - immutable observations with IDs
  4. Reasoning         - hypotheses, agent findings, verification reports
  5. Tools & safety    - tool requests/results, policy decisions, approvals
  6. Mitigation        - proposals and recovery results
  7. Knowledge, memory, context packs
  8. Routing
  9. Provider requests/decisions (the "brain" interface)
 10. Telemetry & run records
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from recap.core.ids import digest_of, utcnow

SCHEMA_VERSION = "recap.v1"


class Contract(BaseModel):
    """Base class for all contracts. Unknown fields are rejected to catch typos early."""

    model_config = ConfigDict(extra="forbid")


# ======================================================================================
# 1. Enums
# ======================================================================================


class IncidentState(StrEnum):
    """Lifecycle states of one incident run. Legal moves live in recap/core/lifecycle.py."""

    CREATED = "CREATED"
    RETRIEVING = "RETRIEVING"
    INVESTIGATING = "INVESTIGATING"
    PROPOSING = "PROPOSING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    MITIGATING = "MITIGATING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    # A free hosted model's quota ran out (HTTP 429 or RECAP's own cap). The run is
    # deferred, not failed: nothing about the incident was decided, and RECAP never
    # switches to another (possibly paid) provider to finish it.
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"


TERMINAL_STATES = frozenset(
    {
        IncidentState.RESOLVED,
        IncidentState.FAILED,
        IncidentState.BLOCKED,
        IncidentState.BUDGET_EXHAUSTED,
        IncidentState.QUOTA_EXHAUSTED,
    }
)


class AgentRole(StrEnum):
    """Who is acting. Only the CONTROLLER may ever execute a write."""

    INVESTIGATOR = "investigator"  # the single generalist used in single-agent mode
    TELEMETRY = "telemetry_investigator"  # health, logs, metrics
    INFRASTRUCTURE = "infrastructure_investigator"  # deployments, events, topology
    VERIFIER = "evidence_verifier"  # checks hypotheses against evidence; no tools
    CONTROLLER = "controller"  # trusted lifecycle owner; the only writer


class SourceType(StrEnum):
    """Where a piece of evidence came from."""

    TOOL = "tool"  # an MCP tool result
    DOC = "doc"  # a retrieved runbook chunk
    MEMORY = "memory"  # a past episode
    ALERT = "alert"  # the incoming alert
    ORACLE = "oracle"  # the independent health oracle


class TrustLevel(StrEnum):
    """Trusted = produced by RECAP's own code. Untrusted = data that might lie or inject."""

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class ToolRisk(StrEnum):
    """Tool taxonomy used by the safety policy."""

    READ = "read"  # no side effects -> allowed automatically
    REVERSIBLE_WRITE = "reversible_write"  # e.g. rollback, restart -> needs exact approval
    HIGH_RISK = "high_risk"  # e.g. scaling to zero -> denied in V1
    DESTRUCTIVE = "destructive"  # e.g. delete namespace -> always denied


class Verdict(StrEnum):
    """Safety gateway decision."""

    ALLOW = "ALLOW"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DENY = "DENY"


class HypothesisStatus(StrEnum):
    PROPOSED = "proposed"  # not checked yet
    SUPPORTED = "supported"  # evidence rules pass
    CONTRADICTED = "contradicted"  # evidence actively disagrees
    INSUFFICIENT = "insufficient"  # not enough evidence either way


class FailureMechanism(StrEnum):
    """Generic failure mechanisms RECAP can reason about in V1."""

    BAD_DEPLOYMENT = "bad_deployment"
    RESOURCE_PRESSURE = "resource_pressure"
    DEPENDENCY_FAILURE = "dependency_failure"  # symptom: a caller fails because a dependency fails
    UNKNOWN = "unknown"


class StrategyMode(StrEnum):
    SINGLE = "single"
    PARALLEL = "parallel"


class EpisodeOutcome(StrEnum):
    SUCCESS = "success"  # ONLY when the independent oracle verified recovery
    FAILED = "failed"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"


class MemoryPartition(StrEnum):
    """Memory isolation label. Evaluation test runs may only read TRAIN episodes.

    TRAIN and TEST belong to the evaluation (a test run never writes, and never reads its
    own split); DEMO holds the scripted policy's runs; LIVE holds runs whose decisions came
    from a real model. A run reads and writes the partitions of its own kind by default.
    """

    TRAIN = "train"
    TEST = "test"
    DEMO = "demo"
    LIVE = "live"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ======================================================================================
# 2. Incident & budget
# ======================================================================================


class RunBudget(Contract):
    """Hard limits for one run. The harness stops with BUDGET_EXHAUSTED when one is hit."""

    max_steps: int = Field(30, ge=1)  # provider decisions across all agents
    max_tool_calls: int = Field(30, ge=0)  # every MCP tool call, reads and writes
    max_elapsed_seconds: float = Field(120.0, gt=0)  # wall clock (approval waits excluded)
    max_estimated_tokens: int = Field(120_000, ge=0)  # sum of context packs sent to providers
    max_parallel_agents: int = Field(2, ge=1)
    # Tokens a model server actually reports (input + output) across the run's requests,
    # reserved before each request and settled with the real count after it (M3).
    max_model_tokens: int = Field(200_000, ge=0)


class Incident(Contract):
    """A typed incident created from an alert. Alert text is untrusted data.

    This full record is for RECAP's trusted code (controller, gateway, verifier, run store).
    Providers get `agent_view()` instead, which leaves out the evaluation labels.
    """

    incident_id: str
    scenario_id: str  # which simulator scenario produced the alert: eval bookkeeping only
    service: str  # primary alerted service
    affected_services: list[str] = Field(default_factory=list)
    symptoms: list[str]
    created_at: datetime
    environment: str  # environment kind, e.g. "simulated"
    metadata: dict[str, Any] = Field(default_factory=dict)
    budget: RunBudget

    def agent_view(self) -> AgentIncident:
        """The incident as a provider (scripted policy or LLM) may see it.

        `scenario_id` names the scenario, and a scenario's name often IS its answer
        ("bad-deployment"), so a provider that read it could skip the investigation.
        `metadata` is left out too: it is free-form, so nothing guarantees it stays label-free.
        """
        return AgentIncident(
            incident_id=self.incident_id,
            service=self.service,
            affected_services=list(self.affected_services),
            symptoms=list(self.symptoms),
            created_at=self.created_at,
            environment=self.environment,
            budget=self.budget,
        )


class AgentIncident(Contract):
    """The provider-facing view of an Incident: the alert as an on-call engineer sees it.

    It has no field for a scenario ID or any other label, so no code path can hand one
    to a provider by accident (extra fields are rejected).
    """

    incident_id: str
    service: str  # primary alerted service
    affected_services: list[str] = Field(default_factory=list)
    symptoms: list[str]
    created_at: datetime
    environment: str  # environment kind, e.g. "simulated"
    budget: RunBudget


class EnvironmentInfo(Contract):
    """What an environment adapter says about itself (shown in the dashboard)."""

    kind: str  # "simulated", "sregym", ...
    name: str
    namespace: str
    is_simulated: bool
    supported_actions: list[str] = Field(default_factory=list)
    description: str = ""
    # How long a fix may take to settle on a LIVE target (pods restart, readiness probes
    # pass) before the recovery oracle's verdict counts, and how often to probe meanwhile.
    # Ignored for simulated targets: they recover the moment a fix is applied.
    stabilization_timeout_seconds: float = Field(60.0, ge=0)
    stabilization_poll_seconds: float = Field(2.0, gt=0)


class ScenarioInfo(Contract):
    """Public description of a scenario. `family`/`expected` are for evaluation only."""

    id: str
    title: str
    description: str = ""
    family: str  # ground-truth label; never shown to agents
    partition: MemoryPartition
    alert_service: str
    tags: list[str] = Field(default_factory=list)
    expected: dict[str, Any] = Field(default_factory=dict)  # eval-only ground truth
    run_overrides: dict[str, Any] = Field(default_factory=dict)
    default_seed: int | None = None  # used when a run does not pick a seed
    budget: dict[str, Any] = Field(default_factory=dict)  # per-scenario RunBudget overrides
    requires_memory_of: list[str] = Field(default_factory=list)  # demo prerequisites


# ======================================================================================
# 3. Evidence
# ======================================================================================


class Evidence(Contract):
    """One immutable observation. Agents share evidence by ID, never by mutable objects."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str  # run-unique, e.g. "ev-3f9a2c-007"
    incident_id: str
    source_type: SourceType
    source_ref: str  # e.g. "mcp:get_service_logs(service=checkout)"
    timestamp: datetime
    content: str  # bounded text rendering (what an LLM would read)
    data: dict[str, Any] = Field(default_factory=dict)  # structured payload (what rules read)
    tool_name: str | None = None
    trust_level: TrustLevel = TrustLevel.UNTRUSTED
    token_estimate: int = 0
    flags: tuple[str, ...] = ()  # e.g. ("prompt_injection_suspected", "truncated")
    agent_role: AgentRole | None = None


# ======================================================================================
# 4. Reasoning: hypotheses, findings, verification
# ======================================================================================


class Hypothesis(Contract):
    """A claim about the root cause that must cite evidence IDs."""

    claim: str
    mechanism: FailureMechanism
    target_service: str | None = None
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    details: dict[str, Any] = Field(default_factory=dict)  # e.g. {"version": "v2.4.1"}
    origin: Literal["investigation", "memory"] = "investigation"
    proposed_by: AgentRole | None = None
    # The model's own words on how sure it is and what would change its mind. Untrusted
    # model text: shown to people, never used to decide anything (no fabricated scores).
    confidence_note: str = ""


class Usage(Contract):
    """Work done. In deterministic mode tokens are *estimates* and cost is always $0."""

    steps: int = 0
    tool_calls: int = 0
    estimated_tokens: int = 0
    actual_input_tokens: int | None = None  # only real LLM providers fill these
    actual_output_tokens: int | None = None
    external_cost_usd: float = 0.0
    is_estimate: bool = True
    unreported_calls: int = 0  # LLM calls that were billed but returned no usage data
    model_requests: int = 0  # HTTP requests actually sent to a language model (incl. retries)

    def plus(self, other: Usage) -> Usage:
        """Return a new Usage that adds `other` to this one."""

        def add_optional(a: int | None, b: int | None) -> int | None:
            return None if a is None and b is None else (a or 0) + (b or 0)

        return Usage(
            steps=self.steps + other.steps,
            tool_calls=self.tool_calls + other.tool_calls,
            estimated_tokens=self.estimated_tokens + other.estimated_tokens,
            actual_input_tokens=add_optional(self.actual_input_tokens, other.actual_input_tokens),
            actual_output_tokens=add_optional(
                self.actual_output_tokens, other.actual_output_tokens
            ),
            external_cost_usd=self.external_cost_usd + other.external_cost_usd,
            is_estimate=self.is_estimate and other.is_estimate,
            unreported_calls=self.unreported_calls + other.unreported_calls,
            model_requests=self.model_requests + other.model_requests,
        )


class AgentFinding(Contract):
    """What one investigator returns after its turn loop."""

    agent_role: AgentRole
    evidence_ids: list[str] = Field(default_factory=list)
    doc_refs: list[str] = Field(default_factory=list)  # runbook chunk IDs the agent saw (citations)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    proposed_next_steps: list[str] = Field(default_factory=list)
    # How the agent ended: with hypotheses, asking for more evidence, by stopping, at its
    # step limit, without any usable model answer, or with an error.
    outcome: Literal[
        "", "hypothesis", "need_more_evidence", "stop", "step_limit", "no_usable_answer", "error"
    ] = ""
    usage: Usage = Field(default_factory=Usage)
    summary: str = ""
    error: str | None = None  # e.g. "timeout" if the agent failed
    started_at: datetime
    finished_at: datetime
    round: int = 1


class HypothesisCheck(Contract):
    """The verifier's result for one hypothesis, with human-readable rule outcomes."""

    hypothesis: Hypothesis  # copy with final status and contradicting IDs filled in
    passed: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    # Every agent that made this claim (agents making the same claim pool their evidence).
    proposers: list[AgentRole] = Field(default_factory=list)
    # The evidence the passing rules relied on. A claim must cite at least one of these
    # (V1-grounding): support it never looked at does not count as its own.
    basis_evidence_ids: list[str] = Field(default_factory=list)


class Disagreement(Contract):
    """Where the agents' claims conflict, and how the verifier's rules settled it, if they did.

    The rules decide, never how many agents agree or how much evidence a claim cites.
      detail_conflict            same mechanism on the same service, but details that cannot
                                 both be true (e.g. which release is bad)
      competing_root_causes      two different actionable root causes are both supported
      cross_agent_contradiction  evidence one agent recorded contradicts another's claim
      missing_view               an agent of this epoch ended without a usable finding
    An open disagreement carries a question for the next round. Its text is built by
    trusted code from checked values only (mechanism names, service names, versions).
    """

    kind: Literal[
        "detail_conflict", "competing_root_causes", "cross_agent_contradiction", "missing_view"
    ]
    claims: list[str] = Field(default_factory=list)  # e.g. "bad deployment on checkout (v2.4.1)"
    agents: list[AgentRole] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)  # what decided it, or what is disputed
    resolved: bool
    rule_id: str = ""  # the rule that settled it; empty while it is open
    resolution: str = ""  # one plain sentence for people
    question: str = ""  # an open disagreement: what the next round must find out


class VerificationReport(Contract):
    """Merged result of all findings. `best` is the top supported, actionable hypothesis."""

    status: Literal["supported", "contradicted", "insufficient"]
    best: Hypothesis | None = None
    checks: list[HypothesisCheck] = Field(default_factory=list)
    unknown_evidence_ids: list[str] = Field(default_factory=list)  # cited but not in this run
    merged_evidence_ids: list[str] = Field(default_factory=list)
    focus_services: list[str] = Field(default_factory=list)  # where to look next
    summary: str = ""
    disagreements: list[Disagreement] = Field(default_factory=list)

    def open_questions(self) -> list[str]:
        """Questions of the disagreements no rule settled, for the next round (trusted text)."""
        return list(
            dict.fromkeys(d.question for d in self.disagreements if not d.resolved and d.question)
        )


class ProposalCheck(Contract):
    """The verifier's structural check of a mitigation proposal."""

    ok: bool
    problems: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)


# ======================================================================================
# 5. Tools & safety
# ======================================================================================


class DiscoveredTool(Contract):
    """A tool as advertised by an MCP server. Everything here is untrusted."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] = Field(default_factory=dict)  # server hints, never trusted


class RawToolResult(Contract):
    """What the MCP transport returns before the gateway bounds and records it."""

    ok: bool
    structured: dict[str, Any] | None = None
    text: str = ""
    error: str | None = None
    # Reference documents a LOCAL read tool found (search_runbooks). The gateway keeps them
    # only for local tools: an MCP server can never put documents into an agent's context.
    docs: list[RetrievedChunk] = Field(default_factory=list)


class ToolSpecView(Contract):
    """What a provider is told about a tool it may use (from the trusted registry)."""

    name: str
    description: str
    risk: ToolRisk
    args: dict[str, Any] = Field(default_factory=dict)
    source: Literal["mcp", "local"] = "mcp"  # local: answered by RECAP itself


class ToolCall(Contract):
    """A provider's request to call one tool."""

    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class ToolCallRecord(Contract):
    """A tool call an agent already made (fed back to the provider as history)."""

    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    evidence_id: str | None = None
    error: str | None = None


class ToolRequest(Contract):
    """One tool call the gateway is about to send: it passed every check; args are validated."""

    invocation_id: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)  # validated arguments
    caller: AgentRole
    incident_id: str
    requested_at: datetime = Field(default_factory=utcnow)


class SafetyDecision(Contract):
    """ALLOW / APPROVAL_REQUIRED / DENY plus the policy rules that produced it."""

    verdict: Verdict
    reasons: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    tool_name: str
    risk: ToolRisk | None = None
    caller: AgentRole | None = None
    policy_version: str = ""
    decided_at: datetime = Field(default_factory=utcnow)


class ToolResult(Contract):
    """A tool call after the gateway: bounded output, timing and the evidence it created."""

    invocation_id: str
    tool_name: str
    ok: bool
    output: dict[str, Any] | None = None
    error: str | None = None
    side_effects: list[str] = Field(default_factory=list)
    duration_ms: float = 0.0
    evidence_id: str | None = None
    truncated: bool = False
    decision: SafetyDecision | None = None
    docs: list[RetrievedChunk] = Field(default_factory=list)  # from a local read tool only


class Approval(Contract):
    """A human (or simulation auto-approver) decision bound to ONE exact proposal.

    It is never general consent: it names the incident, the proposal and its digest,
    expires, and can be consumed only once.
    """

    approval_id: str
    incident_id: str
    proposal_id: str
    proposal_digest: str
    scope: Literal["single_action"] = "single_action"
    decision: Literal["approved", "rejected"]
    actor: str
    decided_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None


ApprovalEventKind = Literal[
    "requested", "approved", "rejected", "revoked", "expired", "consumed", "refused"
]


class ApprovalEvent(Contract):
    """One entry of the append-only approval decision log: who did what to which proposal.

    `revoked` is a rejection that overrode an approval before it was used; `refused`
    records an attempt the store turned down (a late, stale, forged or replayed decision).
    """

    event_id: int
    proposal_id: str
    kind: ApprovalEventKind
    actor: str
    at: datetime
    proposal_digest: str = ""
    detail: str = ""


# ======================================================================================
# 6. Mitigation & recovery
# ======================================================================================


class MitigationDraft(Contract):
    """A remediation suggested by a provider. The controller turns it into a proposal."""

    action: str  # tool name, e.g. "execute_rollback"
    target: str  # usually a service name
    params: dict[str, Any] = Field(default_factory=dict)  # e.g. {"to_version": "v2.4.0"}
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
    expected_health_change: str = ""
    rollback_hint: str = ""
    risk_level: ToolRisk = ToolRisk.REVERSIBLE_WRITE  # declared by the proposer; not trusted
    hypothesis_claim: str | None = None
    source: Literal["runbook", "memory", "fallback", "llm", "redteam"] = "runbook"
    citations: list[str] = Field(default_factory=list)  # doc chunk IDs / episode IDs used


class MitigationProposal(MitigationDraft):
    """A draft with identity. Approvals bind to `proposal_id` + `digest()`."""

    proposal_id: str
    incident_id: str
    created_at: datetime = Field(default_factory=utcnow)

    def digest(self) -> str:
        """Fingerprint of exactly what would be executed (incident, action, target, params)."""
        return digest_of(
            {
                "incident_id": self.incident_id,
                "action": self.action,
                "target": self.target,
                "params": self.params,
            }
        )


class ApprovalRequest(Contract):
    """A pending request for a human decision on one proposal."""

    proposal_id: str
    run_id: str
    incident_id: str
    proposal_digest: str
    proposal: MitigationProposal
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime
    expires_at: datetime


class PolicyRequest(Contract):
    """Everything the safety policy needs to judge one tool call or mitigation."""

    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    caller: AgentRole
    incident_id: str
    environment: EnvironmentInfo
    proposal: MitigationProposal | None = None  # set for mitigations
    approval: Approval | None = None  # set when executing an approved mitigation
    safety_enabled: bool = True  # False only in simulation-only ablations
    now: datetime = Field(default_factory=utcnow)


class OracleProbe(Contract):
    """One independent health observation by the recovery oracle."""

    healthy: bool
    signals: dict[str, Any] = Field(default_factory=dict)  # per-service ground truth
    unhealthy_services: list[str] = Field(default_factory=list)
    checked_at: datetime
    services_checked: list[str] = Field(default_factory=list)


class ActionRecord(Contract):
    """One mitigation attempt and what happened to it."""

    action: str
    target: str
    params: dict[str, Any] = Field(default_factory=dict)
    result: Literal["recovered", "not_recovered", "denied", "rejected", "expired", "error"]
    at: datetime = Field(default_factory=utcnow)
    note: str = ""


class RecoveryResult(Contract):
    """Independent oracle verdict computed from environment state, never from agent text."""

    healthy_before: bool
    healthy_after: bool
    observed_signals: dict[str, Any] = Field(default_factory=dict)
    oracle_name: str
    verified: bool
    checked_at: datetime
    services_checked: list[str] = Field(default_factory=list)


# ======================================================================================
# 7. Knowledge (RAG), memory and context
# ======================================================================================


class DocChunk(Contract):
    """One section of a Markdown runbook, with provenance for citations."""

    chunk_id: str  # stable, e.g. "runbook-rollback#remediation-1"
    doc_path: str  # repo-relative path
    title: str  # document title
    heading: str  # section heading
    text: str
    checksum: str  # sha256 of the source file (first 12 hex chars)
    provenance: dict[str, str] = Field(default_factory=dict)
    token_estimate: int = 0


class RetrievedChunk(Contract):
    chunk: DocChunk
    score: float
    rank: int
    retriever: str  # "bm25" or "hybrid-bm25-embeddings": how this search ranked it
    reranker: str | None = None
    # Why it ranked here: bm25_rank / bm25 (keyword score), dense_rank / cosine (meaning),
    # rrf (the fused score). Only the rankings a search actually used appear.
    signals: dict[str, float] = Field(default_factory=dict)


class RetrievalInfo(Contract):
    """How a run searched the runbooks (RunRecord.retrieval)."""

    mode: Literal["bm25", "hybrid"]
    embedder: str | None = None  # e.g. "ollama:nomic-embed-text" (hybrid only)
    server: dict[str, Any] | None = None  # what Ollama reported: version, weights file, size
    embedding_requests: int = 0  # /api/embed requests answered (live or from a recording)
    embedding_input_tokens: int | None = None  # as the server reported them
    replayed_requests: int = 0  # of those, answered from a recording
    agent_searches: int = 0  # search_runbooks calls answered for the agents


class EvidenceSummary(Contract):
    evidence_id: str
    summary: str


class TrajectoryStep(Contract):
    """One observation a past run made, extracted from its evidence (no model text)."""

    agent: AgentRole | None = None
    call: str  # the canonical source ref, e.g. "mcp:get_service_logs(service=checkout)"
    evidence_id: str
    kind: SourceType = SourceType.TOOL


class ClaimRecord(Contract):
    """One root-cause claim a past run checked, and how the verifier judged it."""

    mechanism: FailureMechanism
    target: str | None = None
    status: HypothesisStatus
    proposers: list[AgentRole] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)  # the rules' own lines (RECAP's text)


class ExperienceEpisode(Contract):
    """A consolidated past run. `outcome=success` requires independent verification.

    The full record is for memory's trusted code (store, partitions, evaluation). Providers
    get `agent_view()`, which keeps only what the past run observed and did.
    """

    episode_id: str
    origin_run_id: str
    scenario_id: str  # eval bookkeeping; never shown to agents
    failure_family: str  # scenario label for isolation/analysis; never shown to agents
    partition: MemoryPartition  # eval bookkeeping; never shown to agents
    service: str
    symptoms: list[str]
    failure_mechanism: FailureMechanism | None = None  # from the verified diagnosis
    mechanism_target: str | None = None
    verified_evidence: list[EvidenceSummary] = Field(default_factory=list)
    unsuccessful_actions: list[ActionRecord] = Field(default_factory=list)
    successful_action: ActionRecord | None = None
    outcome: EpisodeOutcome
    verified: bool = False  # oracle-verified recovery
    verification_source: str | None = None  # oracle name
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    # Since consolidator-v2 (M3). How the summary below was made: by deterministic
    # extraction from the run's own records, never by a model writing about itself.
    summary_method: str = "deterministic-extraction"
    trajectory: list[TrajectoryStep] = Field(default_factory=list)  # what it observed
    checked_claims: list[ClaimRecord] = Field(default_factory=list)  # every claim it judged
    diagnosis_basis: list[str] = Field(default_factory=list)  # the passing rules' lines
    # What must hold for the fix to apply again, e.g. {"action": "execute_rollback",
    # "target": "checkout", "bad_version": "v2.4.1", "to_version": "v2.4.0"}.
    fix_preconditions: dict[str, str] = Field(default_factory=dict)
    # The incident's text embedded by the run's embedder (base64 float32), when it had one.
    embedding: str | None = None
    embedding_model: str | None = None

    def agent_view(self) -> AgentEpisode:
        """The episode as a provider may see it: the observable story, never the answer key.

        `scenario_id` and `failure_family` are the scenario's ground-truth labels, and
        `partition`, `origin_run_id` and `provenance` are evaluation bookkeeping. The
        diagnosis a past run reached (`failure_mechanism`) stays: it is the run's own
        verified conclusion, which is what experience memory is for.
        """
        return AgentEpisode(
            episode_id=self.episode_id,
            service=self.service,
            symptoms=list(self.symptoms),
            failure_mechanism=self.failure_mechanism,
            mechanism_target=self.mechanism_target,
            verified_evidence=list(self.verified_evidence),
            unsuccessful_actions=list(self.unsuccessful_actions),
            successful_action=self.successful_action,
            outcome=self.outcome,
            verified=self.verified,
            verification_source=self.verification_source,
            created_at=self.created_at,
            checks=[step.call for step in self.trajectory if step.kind == SourceType.TOOL],
        )


class AgentEpisode(Contract):
    """The provider-facing view of an ExperienceEpisode. It has no label fields at all."""

    episode_id: str  # kept: providers cite it
    service: str
    symptoms: list[str]
    failure_mechanism: FailureMechanism | None = None
    mechanism_target: str | None = None
    verified_evidence: list[EvidenceSummary] = Field(default_factory=list)
    unsuccessful_actions: list[ActionRecord] = Field(default_factory=list)
    successful_action: ActionRecord | None = None
    outcome: EpisodeOutcome
    verified: bool = False
    verification_source: str | None = None
    created_at: datetime
    checks: list[str] = Field(default_factory=list)  # what the past run observed (source refs)


class MemoryHit(Contract):
    """A retrieved episode. Being *similar* is not the same as being *safe to reuse*."""

    episode: ExperienceEpisode
    similarity: float  # 0..1 lexical similarity to the current incident
    mechanism_match: bool | None = None  # None until checked against live evidence
    outcome: EpisodeOutcome
    safe_to_reuse: bool = False  # success + verified + mechanism matches live evidence
    reason: str = ""
    # How the hit was found: lexical (word overlap, the `similarity` above) and, when the
    # run embeds, semantic (cosine). Only `similarity` drives routing and the scripted policy.
    signals: dict[str, float] = Field(default_factory=dict)
    # After the live check: the conditions that held, and the ones that argue against reuse.
    supporting: list[str] = Field(default_factory=list)
    contradicting: list[str] = Field(default_factory=list)

    def agent_view(self) -> AgentMemoryHit:
        """The hit with its episode reduced to the provider-facing view."""
        return AgentMemoryHit(
            episode=self.episode.agent_view(),
            similarity=self.similarity,
            mechanism_match=self.mechanism_match,
            outcome=self.outcome,
            safe_to_reuse=self.safe_to_reuse,
            reason=self.reason,
            supporting=list(self.supporting),
            contradicting=list(self.contradicting),
        )


class AgentMemoryHit(Contract):
    """The provider-facing view of a MemoryHit (RECAP's reuse verdict plus a label-free episode)."""

    episode: AgentEpisode
    similarity: float
    mechanism_match: bool | None = None
    outcome: EpisodeOutcome
    safe_to_reuse: bool = False
    reason: str = ""
    supporting: list[str] = Field(default_factory=list)
    contradicting: list[str] = Field(default_factory=list)


def _as_agent_incident(value: Any) -> Any:
    """Provider requests accept a full Incident too, but keep only its label-free view."""
    return value.agent_view() if isinstance(value, Incident) else value


def _as_agent_hits(value: Any) -> Any:
    """Likewise for memory hits: each full MemoryHit is reduced to its AgentMemoryHit view."""
    if isinstance(value, list | tuple):
        return [hit.agent_view() if isinstance(hit, MemoryHit) else hit for hit in value]
    return value


# Field types of the provider-facing requests (section 9). Whatever a caller passes in, the
# request ends up holding only the label-free views, so a scenario label cannot reach a
# provider through a request, even by mistake.
ProviderIncident = Annotated[AgentIncident, BeforeValidator(_as_agent_incident)]
ProviderMemoryHits = Annotated[list[AgentMemoryHit], BeforeValidator(_as_agent_hits)]


class ContextItem(Contract):
    """One entry in a bounded context pack. Keeps a pointer to the original record."""

    item_id: str  # evidence ID, doc chunk ID or episode ID
    kind: Literal["evidence", "doc", "memory"]
    source_ref: str
    text: str  # possibly compacted rendering
    token_estimate: int
    score: float
    trust_level: TrustLevel = TrustLevel.UNTRUSTED
    flags: list[str] = Field(default_factory=list)
    compacted: bool = False


class ContextPack(Contract):
    """A bounded, ranked view derived from the evidence registry, docs and memory."""

    items: list[ContextItem] = Field(default_factory=list)
    budget_tokens: int
    tokens_before: int  # total estimated tokens of all candidates
    tokens_after: int  # estimated tokens actually packed
    dropped_ids: list[str] = Field(default_factory=list)

    def ids(self, kind: str | None = None) -> list[str]:
        """IDs of packed items, optionally only one kind."""
        return [i.item_id for i in self.items if kind is None or i.kind == kind]


# ======================================================================================
# 8. Routing
# ======================================================================================


class RoutingSignals(Contract):
    """Observable facts the router uses to choose single vs parallel investigation."""

    services_affected: int
    symptom_categories: list[str] = Field(default_factory=list)
    memory_hits: int = 0
    top_memory_similarity: float = 0.0
    familiar_success: bool = False  # a verified successful episode is very similar
    memory_conflict: bool = False  # similar episodes disagree on mechanism or outcome
    prior_failed_attempts: int = 0
    escalation: bool = False  # the previous investigation round was inconclusive
    # The previous round left a disagreement between agents that no evidence rule settled.
    disagreement: bool = False
    remaining_tool_calls: int
    remaining_steps: int
    max_parallel_agents: int


class StrategyDecision(Contract):
    """The router's choice, with the rule that fired and why."""

    mode: StrategyMode
    reason: str
    rule_id: str
    selected_agents: list[AgentRole]
    provider: str
    budget: dict[str, int] = Field(default_factory=dict)  # per-agent step allocation
    signals: RoutingSignals
    round: int = 1


# ======================================================================================
# 9. Provider requests and decisions
# ======================================================================================


# Where a run's decisions came from. The dashboard shows it as the run-origin badge, so a
# scripted or replayed run can never be mistaken for live model reasoning.
#   scripted         DeterministicProvider: fixed playbooks and heuristics, NOT an LLM
#   local_llm        a model running on this machine (Ollama): no network, no charge
#   free_hosted_llm  a hosted model on a free plan RECAP checks before every call (Groq Free)
#   paid_llm         a billed API (the optional Claude adapter; the owner keeps it disabled)
#   replay           recorded answers of an earlier real LLM run, replayed offline (CI)
ProviderOrigin = Literal["scripted", "local_llm", "free_hosted_llm", "paid_llm", "replay"]


class ProviderInfo(Contract):
    name: str  # e.g. "deterministic-scripted-policy", "ollama:qwen3:8b"
    kind: Literal["scripted", "llm", "replay"]
    origin: ProviderOrigin
    model: str | None = None
    description: str = ""
    # How an LLM answers: "json" (a JSON schema it must follow) or "tools" (native tool calls).
    mode: Literal["json", "tools"] | None = None
    prompt_version: str | None = None  # None for the scripted policy, which uses no prompts
    replay_of: dict[str, Any] | None = None  # a replay's provenance: original provider, model, ...
    recording: str | None = None  # where this run's model exchanges are recorded (or replayed from)
    red_team: bool = False  # a deliberately compromised planner wraps the provider (tests, evals)
    # What the model server reported before the first request, e.g. for Ollama: its version,
    # the model's capabilities, context window, size, quantization and weights digest.
    server: dict[str, Any] | None = None


class ModelCall(Contract):
    """One request sent to a language model, as the trace shows it (`llm.call` events).

    It is the evidence that a decision came from real inference: which model answered, how
    many tokens it read and wrote (as the server reported them, never estimated), how long it
    took and what it said. The answer is model text, so it is untrusted and only an excerpt
    is kept here; the full exchange is in the run's recording (recap/providers/transport.py).
    """

    provider: str  # configured provider name, e.g. "ollama" or "groq"
    model: str  # the model RECAP asked for
    model_reported: str | None = None  # the model the server says answered
    origin: ProviderOrigin
    purpose: Literal["investigate", "propose"]
    mode: Literal["json", "tools"]
    attempt: int  # 1 for the first try, 2 after a corrective retry
    status: int | None = None  # HTTP status; None when no response came back
    ok: bool  # True when the answer could be turned into a decision
    response_kind: str | None = None  # tool_request, hypothesis, need_more_evidence, stop, ...
    input_tokens: int | None = None  # reported by the server; None if it reported nothing
    output_tokens: int | None = None  # includes any reasoning ("thinking") tokens
    duration_ms: float = 0.0
    finish_reason: str | None = None  # e.g. "stop", "length" (cut off), "tool_calls"
    prompt_sha256: str = ""  # fingerprint of the exact messages that were sent
    prompt_chars: int = 0
    output_excerpt: str = ""  # start of the raw answer (untrusted model text)
    thinking_excerpt: str = ""  # start of the model's reasoning, when it returned any
    rate_limit: dict[str, Any] = Field(default_factory=dict)  # hosted only: limit headers
    replayed: bool = False  # True when the answer came from a recording, not a live model
    notes: list[str] = Field(default_factory=list)


class InvestigationRequest(Contract):
    """Everything an investigator's provider may see for one decision.

    Provider-facing: it holds label-free views (AgentIncident, AgentMemoryHit) and nothing
    from the recovery oracle, so a provider cannot read the answer key even if it tried.
    """

    role: AgentRole
    goal: str
    incident: ProviderIncident
    context: ContextPack
    evidence: dict[str, Evidence] = Field(default_factory=dict)  # only packed evidence
    tools: list[ToolSpecView] = Field(default_factory=list)  # read-only tools for this role
    history: list[ToolCallRecord] = Field(default_factory=list)  # this agent's calls so far
    observed_refs: list[str] = Field(default_factory=list)  # source_refs of ALL evidence this run
    step: int
    max_steps: int
    memory_hints: ProviderMemoryHits = Field(default_factory=list)
    # Services to examine first: named by RECAP's evidence verifier or by an earlier fix
    # attempt in this run. Never the oracle's list of unhealthy services.
    focus_services: list[str] = Field(default_factory=list)
    # The role's contract (configs/agents.yaml): what it is responsible for, and the
    # failure mechanisms it may conclude (empty: any). A specialist that cannot see
    # deployments, for example, may not claim a bad release.
    responsibilities: list[str] = Field(default_factory=list)
    mechanisms: list[FailureMechanism] = Field(default_factory=list)
    # Questions RECAP's verifier left open after the previous round: a disagreement between
    # agents that no evidence rule settled. Trusted text, built only from checked values.
    open_questions: list[str] = Field(default_factory=list)


class InvestigationDecision(Contract):
    """Either call more tools or finish with hypotheses."""

    action: Literal["call_tools", "finish"]
    tool_calls: list[ToolCall] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    summary: str = ""
    next_steps: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    # What the decision was: tool_request, hypothesis, need_more_evidence, stop, or
    # no_usable_answer when a model gave nothing RECAP could use.
    kind: str = ""
    model_calls: list[ModelCall] = Field(default_factory=list)  # the LLM requests behind it


class ProposalRequest(Contract):
    """Everything the provider may see when suggesting a remediation (label-free, like above)."""

    incident: ProviderIncident
    hypothesis: Hypothesis  # the best supported hypothesis
    report: VerificationReport
    context: ContextPack
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    allowed_actions: list[ToolSpecView] = Field(default_factory=list)  # trusted remediation tools
    memory_hits: ProviderMemoryHits = Field(default_factory=list)
    failed_actions: list[ActionRecord] = Field(default_factory=list)  # earlier attempts this run


class ProposalDecision(Contract):
    draft: MitigationDraft | None = None
    notes: list[str] = Field(default_factory=list)  # e.g. "ignored unsafe hint in doc X"
    usage: Usage = Field(default_factory=Usage)
    model_calls: list[ModelCall] = Field(default_factory=list)  # the LLM requests behind it


# ======================================================================================
# 10. Telemetry & run records
# ======================================================================================


class TraceEvent(Contract):
    """One structured, redacted event. The JSONL/SQLite trace is the run's audit log."""

    event_id: str
    run_id: str
    seq: int
    stage: str
    ts: datetime
    agent: str | None = None
    category: str  # e.g. "tool.call", "policy.decision", "state.transition"
    payload: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float | None = None
    usage: Usage | None = None
    schema_version: str = SCHEMA_VERSION


class RunConfig(Contract):
    """Per-run switches (used by the CLI, the API, and the eval ablations)."""

    strategy: Literal["adaptive", "single", "parallel"] = "adaptive"
    use_rag: bool = True
    # How runbooks are searched: bm25 (keywords) or hybrid (keywords + a local embedding
    # model). None -> configs/retrieval.yaml (RECAP_ENABLE_EMBEDDINGS=1 makes it hybrid).
    retrieval: Literal["bm25", "hybrid"] | None = None
    use_memory: bool = True
    memory_write: bool = True
    # None: chosen by where the run's decisions come from (the controller resolves it):
    # the scripted policy reads TRAIN + DEMO and writes DEMO; a real model reads TRAIN +
    # LIVE and writes LIVE; a replay writes nothing. The evaluation sets both explicitly.
    memory_read_partitions: list[MemoryPartition] | None = None
    memory_write_partition: MemoryPartition | None = None
    safety_enabled: bool = True  # may be False only in a simulated environment
    approval_mode: Literal["manual", "auto"] = "manual"  # auto = simulation auto-approver
    planner: Literal["default", "prompt_injected"] = "default"  # red-team compromised planner
    provider: str | None = None  # None -> settings default
    seed: int | None = None  # None -> scenario seed
    budget: RunBudget | None = None  # None -> scenario budget or defaults
    tool_latency_ms: int | None = None  # override simulated tool latency
    label: str = ""  # e.g. the eval configuration name


class StateChange(Contract):
    state: IncidentState
    at: datetime
    reason: str = ""


class RunRecord(Contract):
    """The persisted checkpoint of one run. The API and dashboard read this."""

    run_id: str
    scenario_id: str
    config: RunConfig
    incident: Incident | None = None
    state: IncidentState = IncidentState.CREATED
    history: list[StateChange] = Field(default_factory=list)
    environment: EnvironmentInfo | None = None
    provider: str = ""
    provider_info: ProviderInfo | None = None  # origin, model, mode: the run-origin badge
    strategies: list[StrategyDecision] = Field(default_factory=list)
    findings: list[AgentFinding] = Field(default_factory=list)
    verification: VerificationReport | None = None
    retrieved_docs: list[RetrievedChunk] = Field(default_factory=list)
    retrieval: RetrievalInfo | None = None
    memory_hits: list[MemoryHit] = Field(default_factory=list)
    proposals: list[MitigationProposal] = Field(default_factory=list)
    safety_decisions: list[SafetyDecision] = Field(default_factory=list)
    pending_approval: ApprovalRequest | None = None
    approvals: list[Approval] = Field(default_factory=list)
    actions: list[ActionRecord] = Field(default_factory=list)
    recovery: RecoveryResult | None = None
    episode_id: str | None = None
    usage: Usage = Field(default_factory=Usage)
    outcome_note: str = ""
    error: str | None = None
    versions: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
