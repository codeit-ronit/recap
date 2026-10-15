"""Loads the YAML files in configs/ into typed settings.

One `Settings` object is built at startup and passed to the components that need it.
A few environment variables can override defaults (see .env.example). The hash of
all config files is recorded on every run for reproducibility.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field

from recap.core.errors import ConfigError
from recap.core.ids import sha256_hex
from recap.core.models import AgentRole, Contract, RunBudget, ToolRisk

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- Schemas for each config file -------------------------------------------------------


class EnvironmentConfig(Contract):
    """configs/environment.yaml: which TargetEnvironment adapter to build, and its options."""

    kind: str = "simulated"
    options: dict[str, Any] = Field(default_factory=dict)


class ContextSettings(Contract):
    pack_tokens: int = 3000
    max_item_tokens: int = 400


class RoutingSettings(Contract):
    parallel_min_tool_calls: int = 10
    familiar_similarity: float = 0.6
    conflict_similarity: float = 0.45


class RetrySettings(Contract):
    max_investigation_rounds: int = 2
    max_mitigation_attempts: int = 2


class ApprovalSettings(Contract):
    ttl_seconds: int = 900


class BudgetSettings(Contract):
    """configs/budget.yaml."""

    default_budget: RunBudget = Field(default_factory=RunBudget)
    context: ContextSettings = Field(default_factory=ContextSettings)
    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    retries: RetrySettings = Field(default_factory=RetrySettings)
    approvals: ApprovalSettings = Field(default_factory=ApprovalSettings)


class AgentSpec(Contract):
    """One agent role from configs/agents.yaml. All listed tools must be read-only."""

    role: AgentRole
    goal: str
    tools: list[str] = Field(default_factory=list)
    max_steps: int = 8


class ArgRule(Contract):
    """Validation rule for one tool argument (see configs/policies.yaml)."""

    type: Literal["service", "namespace", "version", "int", "enum", "string"]
    required: bool = True
    min: int | None = None
    max: int | None = None
    values: list[str] | None = None


class ToolPolicy(Contract):
    """Trusted description of one tool: its risk tier and argument rules."""

    risk: ToolRisk
    description: str = ""
    args: dict[str, ArgRule] = Field(default_factory=dict)


class PolicyLimits(Contract):
    tool_timeout_seconds: float = 10.0
    max_output_chars: int = 6000
    max_list_items: int = 60


class OracleSettings(Contract):
    max_error_rate: float = 0.05


class PolicySettings(Contract):
    """configs/policies.yaml."""

    version: str
    allowed_namespaces: list[str]
    allowed_services: list[str]
    limits: PolicyLimits = Field(default_factory=PolicyLimits)
    oracle: OracleSettings = Field(default_factory=OracleSettings)
    tools: dict[str, ToolPolicy]
    remediation_tools: list[str]


class ProviderSettings(Contract):
    kind: Literal["scripted", "llm"]
    description: str = ""
    model: str | None = None
    effort: str | None = None
    max_tokens: int | None = None
    price_per_mtok: dict[str, float] | None = None


class ProvidersConfig(Contract):
    """configs/providers.yaml."""

    default: str = "deterministic"
    providers: dict[str, ProviderSettings]


class Features(Contract):
    embeddings: bool = False
    otel: bool = False


class Paths(Contract):
    root: Path
    data_dir: Path
    knowledge_dir: Path
    traces_dir: Path
    sandbox_dir: Path
    recap_db: Path  # runs, events, evidence, approvals
    memory_db: Path  # experience episodes


class Settings(Contract):
    """Everything RECAP needs to start, built from configs/ plus a few env overrides."""

    paths: Paths
    environment: EnvironmentConfig
    budget: BudgetSettings
    agents: dict[AgentRole, AgentSpec]
    policies: PolicySettings
    providers: ProvidersConfig
    features: Features = Field(default_factory=Features)
    config_hash: str


# --- Loading ----------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping")
    return data


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def load_settings(root: Path | None = None, *, data_dir: Path | None = None) -> Settings:
    """Read configs/*.yaml under `root` (the repo root by default) into `Settings`."""
    root = (root or REPO_ROOT).resolve()
    config_dir = root / "configs"
    names = ["environment.yaml", "budget.yaml", "agents.yaml", "policies.yaml", "providers.yaml"]
    raw = {name: _read_yaml(config_dir / name) for name in names}
    config_hash = sha256_hex("".join((config_dir / n).read_text(encoding="utf-8") for n in names))[
        :16
    ]

    data_dir = Path(data_dir or os.environ.get("RECAP_DATA_DIR") or root / "data")
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    paths = Paths(
        root=root,
        data_dir=data_dir,
        knowledge_dir=root / "fixtures" / "knowledge",
        traces_dir=data_dir / "traces",
        sandbox_dir=data_dir / "sandbox",
        recap_db=data_dir / "recap.db",
        memory_db=data_dir / "memory.db",
    )

    environment = EnvironmentConfig.model_validate(raw["environment.yaml"])
    options = dict(environment.options)
    # Resolve paths to absolute ones so the MCP server subprocess sees the same files.
    options["scenarios_dir"] = str(
        (root / options.get("scenarios_dir", "fixtures/scenarios")).resolve()
    )
    options["state_dir"] = str(paths.sandbox_dir)
    if os.environ.get("RECAP_TOOL_LATENCY_MS", "").strip():
        options["tool_latency_ms"] = int(os.environ["RECAP_TOOL_LATENCY_MS"])
    environment = environment.model_copy(update={"options": options})

    agents_raw = raw["agents.yaml"].get("agents", {})
    agents = {
        AgentRole(name): AgentSpec(role=AgentRole(name), **spec)
        for name, spec in agents_raw.items()
    }

    providers = ProvidersConfig.model_validate(raw["providers.yaml"])
    if os.environ.get("RECAP_PROVIDER", "").strip():
        providers = providers.model_copy(update={"default": os.environ["RECAP_PROVIDER"].strip()})
    if providers.default not in providers.providers:
        raise ConfigError(f"unknown provider '{providers.default}'")

    policies = PolicySettings.model_validate(raw["policies.yaml"])
    _check_agents_are_read_only(agents, policies)

    return Settings(
        paths=paths,
        environment=environment,
        budget=BudgetSettings.model_validate(raw["budget.yaml"]),
        agents=agents,
        policies=policies,
        providers=providers,
        features=Features(
            embeddings=_env_flag("RECAP_ENABLE_EMBEDDINGS"), otel=_env_flag("RECAP_ENABLE_OTEL")
        ),
        config_hash=config_hash,
    )


def _check_agents_are_read_only(
    agents: dict[AgentRole, AgentSpec], policies: PolicySettings
) -> None:
    """Refuse to start if a worker agent is configured with a non-read tool."""
    for spec in agents.values():
        for tool in spec.tools:
            policy = policies.tools.get(tool)
            if policy is None:
                raise ConfigError(f"agent {spec.role} lists unknown tool '{tool}'")
            if policy.risk != ToolRisk.READ:
                raise ConfigError(f"agent {spec.role} may not use write tool '{tool}'")
