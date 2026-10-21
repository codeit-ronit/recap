"""Runtime: the composition root that wires RECAP together.

This is the one place that picks concrete implementations for every interface in
recap/core/interfaces.py (environment adapter, MCP transport, provider, retriever,
router, policy, oracle, trace sinks). The CLI demo, the API and the evaluation runner
all create a Runtime and call `run_incident(...)`. To swap a part, change the factory
here or the config file it reads; nothing else needs to change.
"""

from __future__ import annotations

import functools
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

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
from recap.core.models import EnvironmentInfo, RunConfig, RunRecord, ScenarioInfo
from recap.environments.registry import create_environment
from recap.harness.controller import ControllerDeps, IncidentController, TransportFactory
from recap.harness.store import RunStore
from recap.knowledge.retriever import KnowledgeRetriever
from recap.memory.consolidation import Consolidator
from recap.memory.store import ExperienceStore
from recap.providers.factory import create_provider, describe_providers
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
        self.knowledge = KnowledgeRetriever.from_directory(
            paths.knowledge_dir,
            root=paths.root,
            enable_embeddings=self.settings.features.embeddings,
        )
        self.router = RulesRouter(self.settings.budget.routing, agent_specs(self.settings))
        self.verifier = EvidenceVerifier()
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
            environment_factory=self.environment_factory,
            transport_factory=self.transport_factory,
            fixture_hash=self.fixture_hash,
            code_version=self.code_version,
        )

    def make_provider(self, config: RunConfig) -> LLMProvider:
        name = config.provider or self.settings.providers.default
        return create_provider(name, self.settings, planner=config.planner, ledger=self.ledger)

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
        """Which concrete implementation is plugged into each interface right now."""
        try:
            provider: dict[str, Any] = self.make_provider(RunConfig()).info.model_dump(mode="json")
        except ConfigError as exc:  # e.g. the opt-in LLM provider selected without an API key
            provider = {"name": self.settings.providers.default, "error": str(exc)}
        return {
            "environment": self.environment_info().model_dump(mode="json"),
            "environment_kind": self.settings.environment.kind,
            "tool_transport": "MCP over stdio (official Python SDK, subprocess per run)",
            "provider": provider,
            "providers_available": describe_providers(self.settings),
            "retriever": self.knowledge.name,
            "knowledge_chunks": len(self.knowledge.chunks),
            "router": self.router.name,
            "safety_policy": f"{self.policy.name} ({self.policy.version})",
            "verifier": self.verifier.name,
            "oracle": f"{self.settings.environment.kind}-health-oracle",
            "memory_episodes": self.memory.count(),
            "trace_sinks": ["sqlite", "jsonl"]
            + (["live-stream"] if self.hub else [])
            + (["opentelemetry-console"] if self.otel else []),
            "config_hash": self.settings.config_hash,
            "fixture_hash": self.fixture_hash,
            "code_version": self.code_version,
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


def _hash_fixtures(fixtures_dir: Path) -> str:
    """Fingerprint of every scenario and knowledge file (recorded on each run)."""
    parts = [
        f"{path.relative_to(fixtures_dir)}:{sha256_hex(path.read_text(encoding='utf-8'))}"
        for path in sorted(fixtures_dir.rglob("*"))
        if path.is_file()
    ]
    return sha256_hex("|".join(parts))[:16]
