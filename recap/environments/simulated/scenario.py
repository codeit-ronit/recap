"""Scenario files: the typed schema of fixtures/scenarios/<id>.yaml and how to load them.

A scenario describes one simulated incident: the service topology, each service's
deployment history, the fault to inject, extra log lines (noise, misleading hints or
prompt-injection text) and the alert that opens the incident.

`family` and `expected` are ground truth for evaluation only. The simulator never shows
them to agents: `SimulatedEnvironment.reset()` returns nothing but the alert.

Every model forbids unknown fields, so a typo in a fixture fails loudly at load time
instead of silently changing the incident.
"""

from __future__ import annotations

import re
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AwareDatetime, Field, ValidationError, field_validator, model_validator

from recap.core.models import (
    Contract,
    FailureMechanism,
    IncidentState,
    MemoryPartition,
    RunBudget,
    RunConfig,
    ScenarioInfo,
)

SCENARIO_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Service names must match the parser in recap/core/signals.py ("upstream <name> call failed").
SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


class AlertSpec(Contract):
    """The alert that opens the incident (what the on-call engineer is paged with)."""

    service: str
    affected_services: list[str] = Field(default_factory=list)
    symptoms: list[str] = Field(min_length=1)


class ServiceSpec(Contract):
    """One service of the simulated shop and how it behaves when healthy."""

    version: str
    replicas: int = Field(2, ge=1)
    dependencies: list[str] = Field(default_factory=list)  # services this one calls
    memory_limit_mb: int = Field(512, gt=0)
    baseline_memory_mb: int = Field(200, gt=0)
    base_latency_ms: int = Field(90, gt=0)
    base_cpu_pct: int = Field(35, ge=0)


class DeploymentSpec(Contract):
    """One entry of a service's deployment history."""

    service: str
    version: str
    minutes_ago: int = Field(ge=0)
    change: str = ""


class BadReleaseFault(Contract):
    """A release that throws errors and is slow for as long as it is running."""

    type: Literal["bad_release"]
    service: str
    version: str
    started_minutes_ago: int = Field(ge=0)
    error_rate: float = Field(gt=0, le=1)
    latency_ms: int = Field(gt=0)
    exception: str


class MemoryLeakFault(Contract):
    """Memory that grows until the container is OOM-killed again and again."""

    type: Literal["memory_leak"]
    service: str
    started_minutes_ago: int = Field(ge=0)
    oom_minutes_ago: int = Field(ge=0)
    error_rate: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _oom_after_start(self) -> MemoryLeakFault:
        if self.oom_minutes_ago >= self.started_minutes_ago:
            raise ValueError("oom_minutes_ago must be smaller than started_minutes_ago")
        return self


# The `type` field decides which fault model a YAML entry is parsed into.
Fault = Annotated[BadReleaseFault | MemoryLeakFault, Field(discriminator="type")]


class ScriptedLogLine(Contract):
    """An extra log line injected at a fixed minute (noise, misleading or malicious text)."""

    service: str
    minutes_ago: int = Field(ge=0)
    level: Literal["DEBUG", "INFO", "WARN", "ERROR"]
    message: str


class ExpectedOutcome(Contract):
    """Evaluation-only ground truth: the real root cause and the fix that recovers it."""

    root_cause_service: str
    mechanism: FailureMechanism
    fix: str  # remediation tool name, e.g. "execute_rollback"
    fix_params: dict[str, Any] = Field(default_factory=dict)
    final_state: IncidentState | None = None  # only set when a run should NOT end RESOLVED
    redteam: bool = False  # the scenario contains a prompt-injection attempt
    # The attempt is written so that RECAP's pattern scanner does NOT flag it (a plausible
    # "automation" line asking for an allowed action): pattern flags are not a protection.
    evades_scanner: bool = False


class ScenarioSpec(Contract):
    """One complete scenario file."""

    id: str = Field(pattern=SCENARIO_ID.pattern)
    title: str
    description: str = ""
    family: Literal["bad_deployment", "resource_pressure", "misleading_logs"]
    partition: MemoryPartition
    seed: int
    tags: list[str] = Field(default_factory=list)
    incident_time: AwareDatetime
    namespace: str
    alert: AlertSpec
    services: dict[str, ServiceSpec]
    deployments: list[DeploymentSpec]
    faults: list[Fault] = Field(min_length=1)
    log_lines: list[ScriptedLogLine] = Field(default_factory=list)
    # How the shop's services word their own log messages (recap/core/signals.py):
    # "java" (the default templates) or "rpc" (an RPC client's style, M3).
    log_style: Literal["java", "rpc"] = "java"
    expected: dict[str, Any]
    budget: dict[str, Any] = Field(default_factory=dict)  # RunBudget field overrides
    run_overrides: dict[str, Any] = Field(default_factory=dict)  # RunConfig field overrides
    requires_memory_of: list[str] = Field(default_factory=list)  # demo prerequisites

    # The three mappings below stay plain dicts (they are copied into ScenarioInfo and run
    # configs), but each is validated by the typed model it feeds.
    @field_validator("expected")
    @classmethod
    def _check_expected(cls, value: dict[str, Any]) -> dict[str, Any]:
        return ExpectedOutcome.model_validate(value).model_dump(mode="json", exclude_none=True)

    @field_validator("budget")
    @classmethod
    def _check_budget(cls, value: dict[str, Any]) -> dict[str, Any]:
        RunBudget.model_validate(value)
        return value

    @field_validator("run_overrides")
    @classmethod
    def _check_run_overrides(cls, value: dict[str, Any]) -> dict[str, Any]:
        # A scenario is data: it may choose the red-team planner or a strategy, never switch
        # off safety, approvals, memory isolation, the provider or the budget.
        allowed = {"planner", "strategy"}
        if extra := sorted(set(value) - allowed):
            raise ValueError(f"run_overrides may only set {sorted(allowed)}, not {extra}")
        RunConfig.model_validate(value)
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> ScenarioSpec:
        self._check_service_references()
        # Errors propagate along dependencies, so the topology must not contain a cycle.
        TopologicalSorter({n: s.dependencies for n, s in self.services.items()}).prepare()
        self._check_histories()
        return self

    def _check_service_references(self) -> None:
        for name in self.services:
            if not SERVICE_NAME.fullmatch(name):
                raise ValueError(f"invalid service name '{name}'")
        references = [
            ("alert.service", self.alert.service),
            ("expected.root_cause_service", self.expected["root_cause_service"]),
            *(("alert.affected_services", s) for s in self.alert.affected_services),
            *(("dependencies", d) for s in self.services.values() for d in s.dependencies),
            *(("deployments", d.service) for d in self.deployments),
            *(("faults", f.service) for f in self.faults),
            *(("log_lines", line.service) for line in self.log_lines),
        ]
        for where, service in references:
            if service not in self.services:
                raise ValueError(f"{where}: unknown service '{service}'")

    def _check_histories(self) -> None:
        for name, service in self.services.items():
            history = sorted(
                (d for d in self.deployments if d.service == name), key=lambda d: d.minutes_ago
            )
            if not history:
                raise ValueError(f"service '{name}' has no deployment history")
            if history[0].version != service.version:
                raise ValueError(
                    f"service '{name}': latest deployment is {history[0].version}, "
                    f"but the service runs {service.version}"
                )
        for fault in self.faults:
            versions = {d.version for d in self.deployments if d.service == fault.service}
            if isinstance(fault, BadReleaseFault) and fault.version not in versions:
                raise ValueError(f"bad_release version {fault.version} was never deployed")


def load_scenario(scenarios_dir: str | Path, scenario_id: str) -> ScenarioSpec:
    """Load and validate fixtures/scenarios/<scenario_id>.yaml."""
    # Scenario ids can come from the API, so never let one escape the scenarios folder.
    if not SCENARIO_ID.fullmatch(scenario_id):
        raise ValueError(f"invalid scenario id '{scenario_id}'")
    path = Path(scenarios_dir) / f"{scenario_id}.yaml"
    if not path.is_file():
        raise ValueError(f"unknown scenario '{scenario_id}'")
    return _load_file(path)


def list_scenario_specs(scenarios_dir: str | Path) -> list[ScenarioSpec]:
    """Every scenario in the folder, sorted by id."""
    specs = [_load_file(path) for path in Path(scenarios_dir).glob("*.yaml")]
    return sorted(specs, key=lambda spec: spec.id)


def to_info(spec: ScenarioSpec) -> ScenarioInfo:
    """The public catalog entry of a scenario (family/expected are for evaluation only)."""
    return ScenarioInfo(
        id=spec.id,
        title=spec.title,
        description=spec.description,
        family=spec.family,
        partition=spec.partition,
        alert_service=spec.alert.service,
        tags=list(spec.tags),
        expected=dict(spec.expected),
        run_overrides=dict(spec.run_overrides),
        default_seed=spec.seed,  # the controller uses it when a run does not pick a seed
        budget=dict(spec.budget),
        requires_memory_of=list(spec.requires_memory_of),
    )


def _load_file(path: Path) -> ScenarioSpec:
    try:
        spec = ScenarioSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except ValidationError as exc:
        raise ValueError(f"{path.name}: {exc}") from exc
    if spec.id != path.stem:
        raise ValueError(f"{path.name}: id '{spec.id}' must equal the file name")
    return spec
