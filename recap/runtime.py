"""Runtime: the composition root that wires RECAP together.

This is the one place that picks concrete implementations for every interface in
recap/core/interfaces.py (environment adapter, MCP transport, provider, retriever,
router, policy, oracle, trace sinks). The CLI demo, the API and the evaluation runner
all create a Runtime and call `run_incident(...)`. To swap a part, change the factory
here or the config file it reads; nothing else needs to change.
"""

from __future__ import annotations

import functools
import platform
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from recap import __version__
from recap.agents.roles import agent_specs
from recap.agents.verifier import EvidenceVerifier
from recap.context.packer import ContextPacker
from recap.core.config import EnvironmentConfig, Settings, load_settings
from recap.core.errors import ConfigError
from recap.core.ids import sha256_hex
from recap.core.interfaces import (
    ApprovalChannel,
    LLMProvider,
    TargetEnvironment,
    TraceSink,
)
from recap.core.models import (
    SCHEMA_VERSION,
    EnvironmentInfo,
    MemoryPartition,
    RunConfig,
    RunRecord,
    ScenarioInfo,
)
from recap.environments.registry import create_environment
from recap.harness.controller import ControllerDeps, IncidentController, TransportFactory
from recap.harness.store import RunStore
from recap.knowledge.embeddings import Embedder
from recap.knowledge.retriever import BM25, HYBRID, KnowledgeRetriever
from recap.memory.consolidation import Consolidator
from recap.memory.store import ExperienceStore, MemoryAccess
from recap.providers.factory import create_embedder, create_provider, describe_providers
from recap.providers.quota import UsageLedger
from recap.routing.router import RulesRouter
from recap.safety.approvals import ApprovalStore
from recap.safety.policy import PolicyEngine
from recap.telemetry.otel import maybe_otel_sink
from recap.telemetry.redact import Redactor
from recap.telemetry.sinks import BroadcastHub, BroadcastSink, JsonlTraceSink, SqliteTraceSink
from recap.tools.mcp_client import McpToolClient
from recap.tools.registry import ToolRegistry


def stdio_transport(environment: EnvironmentConfig, run_id: str) -> McpToolClient:
    """Default transport: a real MCP server subprocess spoken to over stdio."""
    return McpToolClient.stdio(environment, run_id)


STDIO_TRANSPORT = "MCP over stdio (official Python SDK, subprocess per run)"

# What each environment adapter in this repository really is (GET /system). Only an
# implemented adapter can ever be reported as operational: SREGym is an interface whose every
# operation raises EnvironmentNotIntegrated (milestone M5, docs/status.md).
#   kind: (adapter class or None, implemented, what it is)
ENVIRONMENT_ADAPTERS: dict[str, tuple[str | None, bool, str]] = {
    "simulated": (
        "SimulatedEnvironment",
        True,
        "deterministic local simulation of a five-service shop (recap/environments/simulated/)",
    ),
    "sregym": (
        "SREGymEnvironment",
        False,
        "interface only: every operation raises EnvironmentNotIntegrated "
        "(integrations/sregym/); the real integration is planned for M5",
    ),
    "kind": (
        "KubernetesEnvironment",
        True,
        "a local Kind cluster through typed kubectl operations, four RBAC-limited identities "
        "and an HTTP probe of its own (recap/environments/kubernetes/); chosen with "
        "RECAP_TARGET=kind-demo. GET /system never contacts the cluster: whether it is up "
        "is checked when a run starts",
    ),
}
# Adapters that act on a live target. Being implemented does not make one operational: it
# needs its cluster, which GET /system never contacts, so it counts as operational only
# while it is the configured adapter (and even then a run's preflight decides).
LIVE_ADAPTERS = frozenset({"kind"})


class Runtime:
    """Builds RECAP's long-lived components from configuration and runs incidents."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        hub: BroadcastHub | None = None,
        extra_sinks: Sequence[TraceSink] = (),
        transport_factory: TransportFactory = stdio_transport,
        environment_factory: Callable[
            [EnvironmentConfig, str], TargetEnvironment
        ] = create_environment,
    ) -> None:
        self.settings = settings or load_settings()
        paths = self.settings.paths
        self.hub = hub
        self.extra_sinks = list(extra_sinks)
        self.transport_factory = transport_factory
        self.environment_factory = environment_factory

        self.registry = ToolRegistry(self.settings.policies)
        self.policy = PolicyEngine(self.settings.policies, self.registry)
        self.approvals = ApprovalStore(paths.recap_db)
        self.runs = RunStore(paths.recap_db)
        self.events = SqliteTraceSink(paths.recap_db)
        self.jsonl = JsonlTraceSink(paths.traces_dir)
        self.memory = ExperienceStore(paths.memory_db)
        # Counts every live LLM request and enforces free-tier caps and stops. It is the
        # user's ledger (paths.usage_ledger), shared by every RECAP process and data folder.
        self.ledger = UsageLedger(paths.usage_ledger)
        retrieval = self.settings.retrieval
        vectors = paths.root / retrieval.embedder.vectors
        self.knowledge = KnowledgeRetriever.from_directory(
            paths.knowledge_dir,
            root=paths.root,
            # The stored chunk vectors (scripts/build_vector_index.py); hybrid search is
            # possible only when they cover every chunk (`hybrid_ready`).
            vectors_path=vectors if vectors.is_file() else None,
            min_dense_similarity=retrieval.min_dense_similarity,
            candidate_pool=retrieval.candidate_pool,
            overlays=paths.knowledge_overlays,
        )
        self.router = RulesRouter(self.settings.budget.routing, agent_specs(self.settings))
        # Leads repeat service names in trusted prompt text: only the namespace's allowlist.
        self.verifier = EvidenceVerifier(allowed_services=self.settings.policies.allowed_services)
        self.consolidator = Consolidator()
        self.packer = ContextPacker(self.settings.budget.context.max_item_tokens)
        self.redactor = Redactor()
        self.otel = maybe_otel_sink(self.settings.features.otel)
        self.fixture_hash = _hash_fixtures(paths.root / "fixtures")
        self.code_version = code_version(paths.root)

    # --- running incidents ---------------------------------------------------------------

    async def run_incident(
        self,
        scenario_id: str,
        config: RunConfig | None = None,
        *,
        approval_channel: ApprovalChannel | None = None,
        run_id: str | None = None,
    ) -> RunRecord:
        """Run one incident end to end and return its final checkpoint."""
        controller = IncidentController(
            self.controller_deps(),
            scenario_id=scenario_id,
            config=config or RunConfig(),
            approval_channel=approval_channel,
            run_id=run_id,
        )
        return await controller.run()

    def controller_deps(self) -> ControllerDeps:
        return ControllerDeps(
            settings=self.settings,
            registry=self.registry,
            policy=self.policy,
            approvals=self.approvals,
            knowledge=self.knowledge,
            memory=self.memory,
            router=self.router,
            verifier=self.verifier,
            consolidator=self.consolidator,
            packer=self.packer,
            runs=self.runs,
            trace_sinks=self.trace_sinks,
            redactor=self.redactor,
            provider_factory=self.make_provider,
            embedder_factory=self.make_embedder,
            environment_factory=self.environment_factory,
            transport_factory=self.transport_factory,
            fixture_hash=self.fixture_hash,
            code_version=self.code_version,
        )

    def make_provider(self, config: RunConfig) -> LLMProvider:
        name = config.provider or self.settings.providers.default
        # A replay of a committed recording names it by its manifest id (never a path); the
        # factory checks its bytes against the manifest before anything is read from it.
        return create_provider(
            name,
            self.settings,
            planner=config.planner,
            ledger=self.ledger,
            replay_fixture=config.replay_fixture,
            replay_pace=config.replay_pace,
        )

    def make_embedder(self, provider: LLMProvider) -> Embedder:
        """The run's embedder for hybrid retrieval (recorded and replayed like its model)."""
        return create_embedder(self.settings, provider)

    def trace_sinks(self, run_id: str) -> list[TraceSink]:
        """Every run writes to SQLite and JSONL; plus the live stream and OTel when enabled."""
        sinks: list[TraceSink] = [self.events, self.jsonl, *self.extra_sinks]
        if self.hub is not None:
            sinks.append(BroadcastSink(self.hub))
        if self.otel is not None:
            sinks.append(self.otel)
        return sinks

    # --- introspection (CLI, API, dashboard) ------------------------------------------------

    def environment_info(self) -> EnvironmentInfo:
        return self.environment_factory(self.settings.environment, "describe").describe()

    def list_scenarios(self) -> list[ScenarioInfo]:
        return self.environment_factory(self.settings.environment, "describe").list_scenarios()

    def system_summary(self) -> dict[str, Any]:
        """Which concrete implementation is plugged into each interface right now, and what
        is only planned (GET /system; apps/api/views.py turns it into a typed SystemView).

        Statuses come from the objects this runtime really built and from its settings,
        never from a description: a stub adapter is "planned", a component switched off is
        "disabled", and a provider that cannot be built is "error".
        """
        # Imported here, like the factory does: a run with the scripted policy loads no LLM code.
        from recap.providers.protocol import PROMPT_VERSION

        error = None
        try:
            provider: dict[str, Any] = self.make_provider(RunConfig()).info.model_dump(mode="json")
        except ConfigError as exc:  # e.g. the opt-in LLM provider selected without an API key
            error = str(exc)
            provider = {"name": self.settings.providers.default, "error": error}
        environment = self.environment_info()
        scenarios = len(self.list_scenarios())
        transport = self._transport_name()
        sinks = (
            ["sqlite", "jsonl"]
            + (["live-stream"] if self.hub else [])
            + (["opentelemetry-console"] if self.otel else [])
        )
        return {
            "environment": environment.model_dump(mode="json"),
            "environment_kind": self.settings.environment.kind,
            "tool_transport": transport,
            "provider": provider,
            "providers_available": describe_providers(self.settings),
            # What a run that does not choose searches with, and whether hybrid could run.
            "retriever": HYBRID if self.settings.retrieval.mode == "hybrid" else BM25,
            "retrieval_mode": self.settings.retrieval.mode,
            "hybrid_ready": self.knowledge.hybrid_ready,
            "embedder": self.knowledge.embedder_name,  # the stored vectors' embedder
            "knowledge_chunks": len(self.knowledge.chunks),
            "router": self.router.name,
            "safety_policy": f"{self.policy.name} ({self.policy.version})",
            "verifier": self.verifier.name,
            # The name the oracle gives itself (EnvironmentHealthOracle: "<adapter kind>-...").
            "oracle": f"{environment.kind}-health-oracle",
            "memory_episodes": self.memory.count(),
            "trace_sinks": sinks,
            "config_hash": self.settings.config_hash,
            "fixture_hash": self.fixture_hash,
            "code_version": self.code_version,
            "components": self._components(environment, scenarios, provider, error, sinks),
            "environment_adapters": self._environment_adapters(scenarios),
            "features": self._features(),
            "versions": {
                "recap": __version__,
                "trace_schema": SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,  # what real models are asked with
                "policy_version": self.settings.policies.version,
                "router": self.router.name,
                "verifier": self.verifier.name,
                "config_hash": self.settings.config_hash,
                "fixture_hash": self.fixture_hash,
                "code_version": self.code_version,
                "python": platform.python_version(),
            },
        }

    def _transport_name(self) -> str:
        """What tool calls really go through: the MCP subprocess, or a factory a test or an
        embedding application passed in (named, never passed off as MCP)."""
        if self.transport_factory is stdio_transport:
            return STDIO_TRANSPORT
        name = getattr(self.transport_factory, "__name__", type(self.transport_factory).__name__)
        return f"custom transport factory ({name})"

    def _environment_operational(self, scenarios: int) -> bool:
        """The configured adapter can run an incident: it is implemented (or a custom class)
        and lists at least one scenario to run."""
        known = ENVIRONMENT_ADAPTERS.get(self.settings.environment.kind)
        return (known is None or known[1]) and scenarios > 0

    def _components(
        self,
        environment: EnvironmentInfo,
        scenarios: int,
        provider: dict[str, Any],
        provider_error: str | None,
        sinks: list[str],
    ) -> list[dict[str, Any]]:
        """One entry per interface: the implementation behind it and its real status."""
        operational = self._environment_operational(scenarios)
        retrieval = self.settings.retrieval
        hybrid_default = retrieval.mode == "hybrid"
        ready = self.knowledge.hybrid_ready
        embedder = f"{retrieval.embedder.api}:{retrieval.embedder.model}"
        if provider_error is not None:
            provider_status, provider_detail = "error", provider_error
        else:
            origin = provider.get("origin")
            model = provider.get("model")
            provider_status = "active"
            provider_detail = (
                "the scripted baseline: not an LLM"
                if origin == "scripted"
                else f"{origin}: {model or 'model not named'}"
            )

        def component(
            id_: str, interface: str, implementation: str, status: str, detail: str
        ) -> dict[str, Any]:
            return {
                "id": id_,
                "interface": interface,
                "implementation": implementation,
                "status": status,
                "detail": detail,
            }

        return [
            component(
                "environment",
                "TargetEnvironment",
                f"{environment.name} (kind {environment.kind})",
                "active" if operational else "planned",
                _counted(scenarios, "scenario")
                + "; "
                + ("simulated" if environment.is_simulated else "NOT simulated")
                + ("" if operational else "; not operational: no incident can run on it"),
            ),
            component(
                "tool_transport",
                "ToolTransport",
                self._transport_name(),
                "active",
                "agents reach the environment only through these tools, past the gateway",
            ),
            component(
                "provider",
                "LLMProvider",
                str(provider.get("name") or self.settings.providers.default),
                provider_status,
                provider_detail,
            ),
            component(
                "retriever",
                "RetrievalIndex",
                "BM25 keyword search with a lexical reranker (a heuristic, not a neural model)",
                "active",
                f"{len(self.knowledge.chunks)} runbook chunks; default search: {retrieval.mode}",
            ),
            component(
                "hybrid_retrieval",
                "Embedder",
                f"{embedder} (local, loopback) + reciprocal rank fusion",
                ("active" if hybrid_default else "available") if ready else "unavailable",
                (
                    "stored vectors cover every runbook chunk; a run that asks for hybrid "
                    "search needs the local embedding server then (not checked here)"
                )
                if ready
                else "the stored runbook vectors do not cover every chunk "
                "(scripts/build_vector_index.py)",
            ),
            component(
                "memory",
                "ExperienceStore",
                "SQLite episodes, consolidator v2 (deterministic extraction)",
                "active",
                _counted(self.memory.count(), "episode")
                + " in partitions "
                + ", ".join(p.value for p in MemoryPartition),
            ),
            component(
                "router",
                "RouterPolicy",
                self.router.name,
                "active",
                "single or parallel investigation, from routing signals and the budget",
            ),
            component(
                "safety_policy",
                "SafetyPolicy",
                f"{self.policy.name} ({self.policy.version})",
                "active",
                "judges every tool call and every fix from trusted configuration",
            ),
            component(
                "approvals",
                "ApprovalStore",
                "exact, single-use approvals bound to a proposal digest",
                "active",
                "a person decides through the operator-authenticated API or an interactive "
                "terminal; a rejection wins until the approval is used",
            ),
            component(
                "write_boundary",
                "TargetEnvironment.execute_authorized",
                "Ed25519-signed, single-use capabilities",
                "active" if operational else "planned",
                "the environment refuses any write the gateway did not sign for exactly "
                "that action",
            ),
            component(
                "verifier",
                "Verifier",
                self.verifier.name,
                "active",
                "evidence rules decide which claims are supported; agents never vote",
            ),
            component(
                "oracle",
                "RecoveryOracle",
                f"{environment.kind}-health-oracle",
                "active" if operational else "planned",
                "judges recovery from the environment's own health, never from agent text",
            ),
            component(
                "trace_sinks",
                "TraceSink",
                ", ".join(sinks),
                "active",
                "every run's events in SQLite and JSONL; live ones on the event stream",
            ),
            component(
                "opentelemetry",
                "TraceSink",
                "OpenTelemetry console spans",
                "active" if self.otel else "disabled",
                "optional: RECAP_ENABLE_OTEL=1 with the otel extra installed",
            ),
        ]

    def _environment_adapters(self, scenarios: int) -> list[dict[str, Any]]:
        """Every adapter this repository knows of, plus a custom one if configured."""
        configured = self.settings.environment.kind
        kinds = list(ENVIRONMENT_ADAPTERS)
        if configured not in ENVIRONMENT_ADAPTERS:
            kinds.append(configured)
        adapters = []
        for kind in kinds:
            is_configured = kind == configured
            if kind in ENVIRONMENT_ADAPTERS:
                adapter, implemented, detail = ENVIRONMENT_ADAPTERS[kind]
            else:  # an adapter class named by import path (the tests' FakeEnvironment)
                adapter = kind.rpartition(":")[2] or kind
                implemented = True
                detail = "a custom adapter loaded by import path"
            if not implemented:
                status = "planned"
            else:
                status = "active" if is_configured else "available"
            adapters.append(
                {
                    "kind": kind,
                    "adapter": adapter,
                    "configured": is_configured,
                    "status": status,
                    "operational": implemented
                    and (
                        self._environment_operational(scenarios)
                        if is_configured
                        else kind not in LIVE_ADAPTERS
                    ),
                    "milestone": None if implemented else "M5",
                    "detail": detail,
                }
            )
        return adapters

    def _features(self) -> dict[str, Any]:
        """Switches and capabilities a screen needs to describe this server honestly."""
        retrieval = self.settings.retrieval
        vectors = self.knowledge.vectors
        vector_index = None
        if vectors is not None:
            vector_index = {
                "embedder": vectors.embedder,
                "dimensions": vectors.dimensions,
                **{
                    key: vectors.meta[key]
                    for key in ("model", "weights", "ollama_version", "created_at", "chunks")
                    if key in vectors.meta
                },
            }
        root = self.settings.paths.root
        access = {
            origin: MemoryAccess.for_origin(origin)  # type: ignore[arg-type]
            for origin in ("scripted", "local_llm", "free_hosted_llm", "replay")
        }
        return {
            "retrieval_default": retrieval.mode,
            "hybrid_ready": self.knowledge.hybrid_ready,
            "embeddings_enabled": self.settings.features.embeddings,
            "vector_index": vector_index,
            "knowledge_chunks": len(self.knowledge.chunks),
            # Extra knowledge folders (e.g. a red-team runbook), by repo-relative path or name.
            "knowledge_overlays": [
                str(folder.relative_to(root)) if folder.is_relative_to(root) else folder.name
                for folder in self.settings.paths.knowledge_overlays
            ],
            "agent_search": retrieval.agent_search.model_dump(),
            "memory_partitions": [p.value for p in MemoryPartition],
            "memory_access": {
                origin: {
                    "read": [p.value for p in rule.readable_partitions],
                    "write": rule.write_partition.value if rule.write_enabled else None,
                }
                for origin, rule in access.items()
            },
            "otel": self.settings.features.otel,
        }


@functools.lru_cache(maxsize=8)
def code_version(root: Path) -> str:
    """The git commit RECAP runs from, recorded on every run: "a1b2c3d4e5f6", with "+dirty"
    when tracked files have uncommitted changes; "unknown" outside a git checkout."""

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=5, check=True
        )
        return done.stdout.strip()

    try:
        # Only RECAP's own checkout counts: a copy inside some other repository is "unknown".
        if Path(git("rev-parse", "--show-toplevel")).resolve() != root.resolve():
            return "unknown"
        commit = git("rev-parse", "--short=12", "HEAD")
        # --no-optional-locks: never take the index lock (a concurrent git commit must not fail).
        changed = git("--no-optional-locks", "status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{commit}+dirty" if changed else commit


def _counted(count: int, noun: str) -> str:
    """'1 episode', '3 episodes'."""
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _hash_fixtures(fixtures_dir: Path) -> str:
    """Fingerprint of every scenario and knowledge file (recorded on each run)."""
    parts = [
        f"{path.relative_to(fixtures_dir)}:{sha256_hex(path.read_text(encoding='utf-8'))}"
        for path in sorted(fixtures_dir.rglob("*"))
        if path.is_file()
    ]
    return sha256_hex("|".join(parts))[:16]
