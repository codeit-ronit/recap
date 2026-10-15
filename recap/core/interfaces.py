"""The swappable parts of RECAP ("plugin sockets").

Code elsewhere depends on these interfaces, never on a concrete class, so any part
can be replaced without rewriting the rest of the system.

    Interface          V1 implementation                      Planned upgrade
    -----------------  -------------------------------------  ------------------------------
    TargetEnvironment  SimulatedEnvironment (local sandbox)   SREGym / Kubernetes adapter
    ToolTransport      McpToolClient (real MCP over stdio)    remote MCP servers (HTTP)
    LLMProvider        DeterministicProvider (scripted)       AnthropicProvider, open models
    RetrievalIndex     BM25 (+ optional hybrid embeddings)    neural reranker
    RouterPolicy       RulesRouter                            LearnedRouter (contextual bandit)
    SafetyPolicy       PolicyEngine (simulator scope)         Kubernetes RBAC-aware policy
    RecoveryOracle     EnvironmentHealthOracle                benchmark health oracle
    Verifier           EvidenceVerifier (evidence rules)      richer causal checks
    TraceSink          JSONL / SQLite / live broadcast        OpenTelemetry collector
    ApprovalChannel    auto (simulation) / console / API      chat-ops approvals
    TrainingPipeline   NOT implemented (by design)            trajectory distillation / SFT

The most important one is TargetEnvironment: it is the only code that knows what the
infrastructure is. Agents reach it through MCP tools; the oracle reaches it through
`health_check`. Switching from the simulator to SREGym or Kubernetes means writing one
adapter and changing configs/environment.yaml: the controller, gateway, RAG, memory,
router and orchestrator code stay the same. What is still simulator-specific is
*data*, not code: the log conventions the scripted policy and the verifier parse
(recap/core/signals.py), the namespace/service allowlists and the runbooks. A real target
needs adapted conventions (or an LLM provider), allowlists and runbooks.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

from recap.core.models import (
    AgentFinding,
    ApprovalRequest,
    DiscoveredTool,
    EnvironmentInfo,
    Incident,
    InvestigationDecision,
    InvestigationRequest,
    MitigationDraft,
    OracleProbe,
    PolicyRequest,
    ProposalCheck,
    ProposalDecision,
    ProposalRequest,
    ProviderInfo,
    RawToolResult,
    RetrievedChunk,
    RoutingSignals,
    RunConfig,
    SafetyDecision,
    ScenarioInfo,
    StrategyDecision,
    TraceEvent,
    VerificationReport,
)
from recap.core.observations import (
    ActionOutcome,
    AlertSignal,
    AuthorizedAction,
    DeploymentList,
    EventList,
    HealthReport,
    HealthSnapshot,
    LogQueryResult,
    MetricName,
    MetricSeries,
    RollbackPlan,
    ServiceList,
)

if TYPE_CHECKING:
    from recap.context.evidence import EvidenceRegistry


class TargetEnvironment(ABC):
    """The system RECAP investigates and repairs (simulator now, SREGym/Kubernetes later).

    Trust boundary:
      * `reset` and `health_check` are called only by trusted controller code.
      * `observe_*` and `plan_rollback` are read-only and are exposed to agents as MCP tools.
      * `execute_authorized` is the single write path. It is reached only through the MCP
        server *after* the central safety gateway approved the exact action.
    """

    kind: ClassVar[str]  # e.g. "simulated"

    # --- lifecycle (trusted) -------------------------------------------------------------
    @abstractmethod
    async def reset(self, scenario_id: str, seed: int) -> AlertSignal:
        """Prepare a fresh incident (inject the scenario's fault) and return its alert."""

    @abstractmethod
    def describe(self) -> EnvironmentInfo:
        """Static description of this environment."""

    @abstractmethod
    def list_scenarios(self) -> list[ScenarioInfo]:
        """Scenarios/problems this environment can reset into."""

    # --- observe (read-only; exposed to agents via MCP) -----------------------------------
    @abstractmethod
    async def observe_services(self) -> ServiceList: ...

    @abstractmethod
    async def observe_health(self, service: str) -> HealthSnapshot: ...

    @abstractmethod
    async def observe_logs(
        self, service: str, limit: int, since_minutes: int
    ) -> LogQueryResult: ...

    @abstractmethod
    async def observe_metrics(
        self, service: str, metric: MetricName, window_minutes: int
    ) -> MetricSeries: ...

    @abstractmethod
    async def observe_deployments(self, service: str | None, limit: int) -> DeploymentList: ...

    @abstractmethod
    async def observe_events(self, service: str | None, limit: int) -> EventList: ...

    @abstractmethod
    async def plan_rollback(self, service: str) -> RollbackPlan:
        """Dry run: which version a rollback would restore. No side effects."""

    # --- act (single write path) ----------------------------------------------------------
    @abstractmethod
    async def execute_authorized(self, action: AuthorizedAction) -> ActionOutcome:
        """Apply an action the gateway already authorized."""

    # --- oracle (trusted, independent of agents) ------------------------------------------
    @abstractmethod
    async def health_check(self) -> HealthReport:
        """Ground-truth health of every service, read directly from environment state."""


@runtime_checkable
class ToolTransport(Protocol):
    """How the gateway talks to a tool server. V1: a real MCP client over stdio."""

    transport_name: str

    async def list_tools(self) -> list[DiscoveredTool]: ...

    async def call_tool(
        self, name: str, args: dict[str, object], timeout_seconds: float
    ) -> RawToolResult: ...


@runtime_checkable
class LLMProvider(Protocol):
    """The 'brain' that picks tool calls and suggests remediations.

    V1 default is a scripted deterministic policy (not an LLM). Real LLM adapters must
    return the same validated decision types, so the harness cannot tell them apart.
    """

    info: ProviderInfo

    async def next_action(self, request: InvestigationRequest) -> InvestigationDecision: ...

    async def propose_mitigation(self, request: ProposalRequest) -> ProposalDecision: ...


@runtime_checkable
class RetrievalIndex(Protocol):
    """Searches the static knowledge base (runbooks) and returns cited chunks."""

    name: str

    def search(self, query: str, k: int = 4) -> list[RetrievedChunk]: ...


@runtime_checkable
class Reranker(Protocol):
    """Re-orders retrieved chunks. V1 uses a simple lexical reranker."""

    name: str

    def rerank(self, query: str, results: list[RetrievedChunk]) -> list[RetrievedChunk]: ...


@runtime_checkable
class RouterPolicy(Protocol):
    """Chooses single vs parallel investigation from observable signals."""

    name: str

    def decide(
        self, signals: RoutingSignals, config: RunConfig, round_number: int
    ) -> StrategyDecision: ...


@runtime_checkable
class SafetyPolicy(Protocol):
    """Trusted code that decides ALLOW / APPROVAL_REQUIRED / DENY for any tool call."""

    name: str
    version: str

    def evaluate(self, request: PolicyRequest) -> SafetyDecision: ...


@runtime_checkable
class RecoveryOracle(Protocol):
    """Independent judge of recovery. Reads environment state, never agent output."""

    name: str

    async def probe(self) -> OracleProbe: ...


@runtime_checkable
class Verifier(Protocol):
    """Trusted checker of hypotheses and proposals against the evidence recorded in a run.

    V1 uses structural evidence rules (not proof of causation). It never calls tools and
    never trusts a provider's claims: every cited evidence ID must exist in this run.
    """

    name: str

    def verify(
        self, findings: Sequence[AgentFinding], registry: EvidenceRegistry, incident: Incident
    ) -> VerificationReport: ...

    def check_proposal(
        self, proposal: MitigationDraft, report: VerificationReport, registry: EvidenceRegistry
    ) -> ProposalCheck: ...


@runtime_checkable
class TraceSink(Protocol):
    """Somewhere trace events are written (file, database, live stream, OTel)."""

    def write(self, event: TraceEvent) -> None: ...


@runtime_checkable
class ApprovalChannel(Protocol):
    """Who answers an approval request: a human (console/API) or the simulation auto-approver.

    The channel only *notifies*; the decision is always recorded and validated by the
    ApprovalStore so every channel goes through the same checks.
    """

    name: str

    async def notify(self, request: ApprovalRequest) -> None: ...


class TrainingPipeline(ABC):
    """Placeholder for future trajectory distillation / fine-tuning. NOT implemented in V1.

    It exists so the architecture shows where learning-from-trajectories would plug in.
    Nothing in V1 trains or fine-tunes a model.
    """

    @abstractmethod
    def train(self, episodes_path: str) -> None:
        raise NotImplementedError("TrainingPipeline is not implemented in RECAP V1.")
