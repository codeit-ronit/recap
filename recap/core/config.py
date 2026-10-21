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
from pydantic import Field, model_validator

from recap.core.errors import ConfigError
from recap.core.ids import sha256_hex
from recap.core.models import (
    AgentRole,
    Contract,
    FailureMechanism,
    ProviderOrigin,
    RunBudget,
    ToolRisk,
)

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
    # In a parallel round each agent may use at most this share of the run's remaining
    # time: a slow specialist then ends as an error finding, and the other's survives.
    parallel_agent_time_share: float = Field(default=0.7, gt=0, le=1)


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
    # The role's contract: shown to a model as its responsibilities, and the failure
    # mechanisms it may conclude (empty: any). Enforced by the harness for every provider.
    responsibilities: list[str] = Field(default_factory=list)
    mechanisms: list[FailureMechanism] = Field(default_factory=list)


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
    # mcp: served by the environment's MCP server; local: answered by RECAP itself (a read
    # over its own data, such as the runbook index). A server can never serve a local tool.
    source: Literal["mcp", "local"] = "mcp"

    @model_validator(mode="after")
    def _local_tools_only_read(self) -> ToolPolicy:
        if self.source == "local" and self.risk != ToolRisk.READ:
            raise ValueError("a local tool must be a read tool")
        return self


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


class QuotaCaps(Contract):
    """RECAP's own limits for a free hosted model, set BELOW the provider's published free
    limits, so RECAP stops itself before the provider has to refuse (recap/providers/quota.py)."""

    requests_per_minute: int = Field(ge=1)
    requests_per_day: int = Field(ge=1)
    tokens_per_minute: int = Field(ge=1)
    tokens_per_day: int = Field(ge=1)
    requests_per_run: int = Field(ge=1)


class FreeTierSettings(Contract):
    """How a hosted provider is kept at $0 (configs/providers.yaml). Checked before every call."""

    api_key_env: str  # name of the env var holding the key; keys are never read from files
    # Name of the env var in which the OWNER records the date (YYYY-MM-DD) they checked the
    # account's billing page and saw the free plan with no payment method.
    attestation_env: str
    attestation_max_age_days: int = Field(7, ge=1)
    allowed_models: list[str]  # exact model IDs; a request for anything else is refused
    # model -> requests per day on the FREE plan, from the provider's docs. Every response's
    # x-ratelimit-limit-requests header must match it (a paid plan has a different limit).
    free_daily_request_limit: dict[str, int]
    limits_checked_on: str  # when the free limits above were read in the provider's docs
    caps: QuotaCaps
    # Tokens reserved for the ANSWER when checking RECAP's own caps (the prompt is estimated
    # separately). An answer may use up to max_output_tokens, but reserving that much would
    # let only one request through a minute; the answer's real size replaces the
    # reservation as soon as it arrives, and the provider's own limit sits above the cap.
    reserved_output_tokens: int = Field(1024, ge=1)
    max_retry_after_seconds: float = 30.0  # after a 429, wait at most this long, once


class ProviderSettings(Contract):
    """One entry of configs/providers.yaml: a provider RECAP can be told to use."""

    kind: Literal["scripted", "llm", "replay"]
    origin: ProviderOrigin  # where decisions come from (shown as the run-origin badge)
    description: str = ""
    model: str | None = None
    # --- the optional Claude adapter (paid; the owner keeps it disabled) ---
    effort: str | None = None
    max_tokens: int | None = None
    price_per_mtok: dict[str, float] | None = None
    # --- LLMs RECAP talks to over plain HTTP: Ollama and OpenAI-compatible hosts ---
    api: Literal["ollama", "openai_compatible"] | None = None
    base_url: str | None = None
    mode: Literal["json", "tools"] = "json"  # investigation steps: JSON schema or native tools
    think: bool = False  # Ollama: let a "thinking" model reason before it answers
    reasoning_effort: str | None = None  # hosted reasoning models (gpt-oss): low/medium/high
    context_tokens: int | None = None  # Ollama num_ctx: the context window to allocate
    max_output_tokens: int = Field(1024, ge=16)
    temperature: float = 0.0
    seed: int | None = 7
    request_timeout_seconds: float = Field(300.0, gt=0)
    max_attempts: int = Field(2, ge=1, le=3)  # per decision: first try + corrective retries
    record: bool = True  # record every model exchange under data/recordings/ (for replay)
    budget: dict[str, Any] = Field(default_factory=dict)  # RunBudget overrides for this provider
    free_tier: FreeTierSettings | None = None  # required for origin free_hosted_llm
    recording: str | None = None  # replay: the recording to answer from (repo-relative)

    @model_validator(mode="after")
    def _consistent(self) -> ProviderSettings:
        """Refuse half-configured providers at startup instead of failing mid-run."""
        unknown = set(self.budget) - set(RunBudget.model_fields)
        if unknown:
            raise ValueError(f"unknown budget fields: {', '.join(sorted(unknown))}")
        if self.kind == "llm" and self.origin in ("local_llm", "free_hosted_llm"):
            if self.api is None or not self.base_url or not self.model:
                raise ValueError("an HTTP LLM provider needs api, base_url and model")
        # The origin is what the run badge and the cost line say, so it must match what the
        # code really does: Ollama is local, an OpenAI-compatible host is the free hosted
        # provider (the only hosted kind RECAP supports), and nothing else may claim either.
        expected_origin = {"ollama": "local_llm", "openai_compatible": "free_hosted_llm"}
        if self.api is not None and self.origin != expected_origin[self.api]:
            raise ValueError(f"api {self.api!r} must have origin {expected_origin[self.api]!r}")
        if self.origin in ("local_llm", "free_hosted_llm") and self.api is None:
            raise ValueError(f"origin {self.origin!r} needs an api")
        if self.origin == "free_hosted_llm" and not (self.base_url or "").startswith("https://"):
            raise ValueError("a hosted provider's base_url must use https:// (the key is sent)")
        if self.origin == "free_hosted_llm" and self.free_tier is None:
            raise ValueError("a free hosted provider needs a free_tier section")
        if self.origin == "free_hosted_llm" and self.free_tier is not None:
            if self.model not in self.free_tier.allowed_models:
                raise ValueError(f"model {self.model!r} is not in free_tier.allowed_models")
        if self.kind == "replay" and self.origin != "replay":
            raise ValueError("a replay provider must have origin 'replay'")
        return self


class ProvidersConfig(Contract):
    """configs/providers.yaml."""

    default: str = "deterministic"
    providers: dict[str, ProviderSettings]


class EmbedderSettings(Contract):
    """The local embedding model behind hybrid retrieval (configs/retrieval.yaml)."""

    api: Literal["ollama"] = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "nomic-embed-text"
    dimensions: int = Field(default=768, gt=0)
    query_prefix: str = "search_query: "
    document_prefix: str = "search_document: "
    request_timeout_seconds: float = Field(default=60.0, gt=0)
    vectors: str = "fixtures/knowledge/vectors/nomic-embed-text.json"  # repo-relative


class AgentSearchSettings(Contract):
    """The search_runbooks tool's own budget."""

    max_per_run: int = Field(default=6, ge=0)
    k: int = Field(default=3, ge=1, le=8)


class RetrievalSettings(Contract):
    """configs/retrieval.yaml."""

    mode: Literal["bm25", "hybrid"] = "bm25"
    k: int = Field(default=4, ge=1)
    candidate_pool: int = Field(default=20, ge=1)
    min_dense_similarity: float = Field(default=0.55, ge=-1, le=1)
    embedder: EmbedderSettings = Field(default_factory=EmbedderSettings)
    agent_search: AgentSearchSettings = Field(default_factory=AgentSearchSettings)


class Features(Contract):
    embeddings: bool = False  # RECAP_ENABLE_EMBEDDINGS=1: hybrid retrieval by default
    otel: bool = False


class Paths(Contract):
    root: Path
    data_dir: Path
    knowledge_dir: Path
    traces_dir: Path
    sandbox_dir: Path
    recap_db: Path  # runs, events, evidence, approvals
    memory_db: Path  # experience episodes
    # LLM usage and free-tier stops: per USER, outside every data folder, so every RECAP
    # process (CLI, API, any RECAP_DATA_DIR) shares the same caps and tripwire stops.
    usage_ledger: Path


class Settings(Contract):
    """Everything RECAP needs to start, built from configs/ plus a few env overrides."""

    paths: Paths
    environment: EnvironmentConfig
    budget: BudgetSettings
    agents: dict[AgentRole, AgentSpec]
    policies: PolicySettings
    providers: ProvidersConfig
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
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


def _usage_ledger_path() -> Path:
    """RECAP_USAGE_LEDGER, else $XDG_STATE_HOME/recap/llm-usage.db (~/.local/state/...).

    Deliberately NOT under the data folder: a free-plan stop or a daily cap must hold for
    every RECAP process of this user, whatever RECAP_DATA_DIR it runs with.
    """
    configured = os.environ.get("RECAP_USAGE_LEDGER", "").strip()
    if configured:
        return Path(configured).expanduser()
    state = os.environ.get("XDG_STATE_HOME", "").strip() or str(Path.home() / ".local" / "state")
    return Path(state).expanduser() / "recap" / "llm-usage.db"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def load_settings(root: Path | None = None, *, data_dir: Path | None = None) -> Settings:
    """Read configs/*.yaml under `root` (the repo root by default) into `Settings`."""
    root = (root or REPO_ROOT).resolve()
    config_dir = root / "configs"
    names = [
        "environment.yaml",
        "budget.yaml",
        "agents.yaml",
        "policies.yaml",
        "providers.yaml",
        "retrieval.yaml",
    ]
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
        usage_ledger=_usage_ledger_path(),
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

    retrieval = RetrievalSettings.model_validate(raw["retrieval.yaml"].get("retrieval", {}))
    embeddings = _env_flag("RECAP_ENABLE_EMBEDDINGS")
    if embeddings:  # the owner's switch: hybrid retrieval by default (a run can still choose)
        retrieval = retrieval.model_copy(update={"mode": "hybrid"})

    return Settings(
        paths=paths,
        environment=environment,
        budget=BudgetSettings.model_validate(raw["budget.yaml"]),
        agents=agents,
        policies=policies,
        providers=providers,
        retrieval=retrieval,
        features=Features(embeddings=embeddings, otel=_env_flag("RECAP_ENABLE_OTEL")),
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
