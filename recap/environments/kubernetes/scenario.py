"""Kind scenarios: the typed schema of fixtures/kind-scenarios/<id>.yaml.

A Kind scenario names the baseline every service is put back on, the typed fault steps the
trusted reset applies to the real cluster (as the recap-scenario identity), and the alert
rule reset waits for. There is no free-form command anywhere: a step is a new image version
for one demo service, or an HTTP burst to one of its paths.

`family` and `expected` are ground truth for evaluation only, exactly as in the simulator's
scenario files: reset returns nothing but the measured alert. Unknown fields are refused,
so a typo fails at load time instead of silently changing the incident.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from recap.core.models import Contract, MemoryPartition, RunBudget, RunConfig, ScenarioInfo
from recap.environments.kubernetes.options import DNS_LABEL, URL_PATH
from recap.environments.kubernetes.probe import AlertRule
from recap.environments.simulated.scenario import ExpectedOutcome

SCENARIO_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VERSION = r"^v[0-9]+\.[0-9]+\.[0-9]+$"


class SetImage(Contract):
    """Release a version of one demo service (the image <repository>/<service>:<version>)."""

    type: Literal["set_image"]
    service: str
    version: str = Field(pattern=VERSION)


class HttpBurst(Contract):
    """Send a burst of GET requests to one path of a service (e.g. the /report burst)."""

    type: Literal["http_burst"]
    service: str
    path: str = Field(pattern=URL_PATH.pattern)
    requests: int = Field(ge=1, le=5000)
    concurrency: int = Field(8, ge=1, le=32)


FaultStep = Annotated[SetImage | HttpBurst, Field(discriminator="type")]


class KindScenarioSpec(Contract):
    """One Kind scenario file."""

    id: str = Field(pattern=SCENARIO_ID.pattern)
    title: str
    description: str = ""
    family: Literal["bad_deployment", "resource_pressure", "misleading_logs"]
    partition: MemoryPartition
    seed: int = 0  # recorded with the run; a real cluster is not deterministic
    tags: list[str] = Field(default_factory=list)
    baseline: dict[str, str]  # service -> the version reset restores before the fault
    faults: list[FaultStep] = Field(min_length=1)
    alert: AlertRule
    expected: dict[str, Any]  # evaluation only: never reaches an agent
    budget: dict[str, Any] = Field(default_factory=dict)  # RunBudget field overrides
    run_overrides: dict[str, Any] = Field(default_factory=dict)  # planner / strategy only
    requires_memory_of: list[str] = Field(default_factory=list)

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
        # A scenario is data: it may choose a planner or a strategy, never switch off safety,
        # approvals, memory isolation, the provider or the budget.
        allowed = {"planner", "strategy"}
        if extra := sorted(set(value) - allowed):
            raise ValueError(f"run_overrides may only set {sorted(allowed)}, not {extra}")
        RunConfig.model_validate(value)
        return value

    @model_validator(mode="after")
    def _check_services(self) -> KindScenarioSpec:
        for service, version in self.baseline.items():
            if not DNS_LABEL.fullmatch(service) or not re.fullmatch(VERSION, version):
                raise ValueError(f"baseline: {service!r} must map to a vX.Y.Z version")
        references = [
            ("alert.service", self.alert.service),
            ("expected.root_cause_service", self.expected["root_cause_service"]),
            *(("faults", step.service) for step in self.faults),
        ]
        for where, service in references:
            if service not in self.baseline:
                raise ValueError(f"{where}: {service!r} has no baseline version")
        return self


def load_kind_scenario(scenarios_dir: str | Path, scenario_id: str) -> KindScenarioSpec:
    """Load and validate <scenarios_dir>/<scenario_id>.yaml."""
    # Scenario ids can come from the API: never let one escape the scenarios folder.
    if not isinstance(scenario_id, str) or not SCENARIO_ID.fullmatch(scenario_id):
        raise ValueError(f"invalid scenario id {str(scenario_id)[:64]!r}")
    path = Path(scenarios_dir) / f"{scenario_id}.yaml"
    if not path.is_file():
        raise ValueError(f"unknown scenario '{scenario_id}'")
    return _load_file(path)


def list_kind_scenarios(scenarios_dir: str | Path) -> list[KindScenarioSpec]:
    """Every scenario in the folder, sorted by id."""
    specs = [_load_file(path) for path in Path(scenarios_dir).glob("*.yaml")]
    return sorted(specs, key=lambda spec: spec.id)


def to_info(spec: KindScenarioSpec) -> ScenarioInfo:
    """The catalog entry (family and expected are for evaluation only)."""
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
        default_seed=spec.seed,
        budget=dict(spec.budget),
        requires_memory_of=list(spec.requires_memory_of),
    )


def _load_file(path: Path) -> KindScenarioSpec:
    try:
        spec = KindScenarioSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except ValidationError as exc:
        raise ValueError(f"{path.name}: {exc}") from exc
    if spec.id != path.stem:
        raise ValueError(f"{path.name}: id '{spec.id}' must equal the file name")
    return spec
