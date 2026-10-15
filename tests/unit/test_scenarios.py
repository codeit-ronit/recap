"""Tests for the scenario fixtures (fixtures/scenarios) and their schema (scenario.py).

Fixtures are data, so they get their own checks: every file loads, alerts do not leak the
answer, everything stays inside the trusted policy allowlist, red-team text is actually
flagged, and, most importantly, every scenario is broken at reset and recovered by its
expected fix (so evaluation ground truth is consistent with the simulator).
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from recap.core.config import REPO_ROOT, Settings
from recap.core.models import MemoryPartition
from recap.core.observations import AuthorizedAction
from recap.environments.simulated import SimulatedEnvironment
from recap.environments.simulated.scenario import (
    ScenarioSpec,
    list_scenario_specs,
    load_scenario,
    to_info,
)
from recap.safety.injection import scan_text
from recap.verification.oracle import EnvironmentHealthOracle, recovery_result
from tests.helpers import act

SCENARIOS = REPO_ROOT / "fixtures" / "scenarios"
SPECS = list_scenario_specs(SCENARIOS)
ROOT_CAUSE_WORDS = (
    "deploy",
    "release",
    "rollback",
    "roll back",
    "oom",
    "memory leak",
    "crash loop",
    "crashloop",
)
VERSION = re.compile(r"\bv\d+\.\d+")
DEMOS = {
    "bad-deployment",
    "ambiguous-incident",
    "memory-caution",
    "malicious-log",
    "budget-exhausted",
    "resource-pressure",
}

# The same mapping the MCP server uses for its write tools.
FIX_ACTIONS = {"execute_rollback": "rollback", "restart_service": "restart"}


def by_id(spec: ScenarioSpec) -> str:
    return spec.id


async def reset(spec: ScenarioSpec, tmp_path: Path) -> SimulatedEnvironment:
    """A fresh simulated environment reset into `spec` with its own seed."""
    env = SimulatedEnvironment(
        run_id="run-fixture", scenarios_dir=str(SCENARIOS), state_dir=str(tmp_path)
    )
    await env.reset(spec.id, spec.seed)
    return env


# --- the fixture set as a whole ---------------------------------------------------------


def test_all_fifteen_scenarios_load_and_ids_match_file_names() -> None:
    stems = sorted(path.stem for path in SCENARIOS.glob("*.yaml"))
    assert len(stems) == 15
    assert [spec.id for spec in SPECS] == stems  # list_scenario_specs sorts by id
    assert all(load_scenario(SCENARIOS, stem).id == stem for stem in stems)


def test_partitions_split_demo_train_and_test() -> None:
    counts = Counter(spec.partition for spec in SPECS)
    assert counts == {MemoryPartition.DEMO: 6, MemoryPartition.TRAIN: 3, MemoryPartition.TEST: 6}
    assert {spec.id for spec in SPECS if spec.partition == MemoryPartition.DEMO} == DEMOS


def test_seeds_and_incident_times_are_distinct() -> None:
    assert len({spec.seed for spec in SPECS}) == len(SPECS)
    assert len({spec.incident_time for spec in SPECS}) == len(SPECS)


def test_demo_prerequisites_and_overrides() -> None:
    ids = {spec.id for spec in SPECS}
    for spec in SPECS:
        assert set(spec.requires_memory_of) <= ids
    specs = {spec.id: spec for spec in SPECS}
    assert specs["memory-caution"].requires_memory_of == ["bad-deployment"]
    assert specs["budget-exhausted"].budget == {"max_tool_calls": 3}
    assert specs["budget-exhausted"].expected["final_state"] == "BUDGET_EXHAUSTED"
    assert specs["malicious-log"].run_overrides == {"planner": "prompt_injected"}
    assert specs["test-cart-injection"].run_overrides == {}  # the evaluation picks the planner


# --- per scenario -----------------------------------------------------------------------


@pytest.mark.parametrize("spec", SPECS, ids=by_id)
def test_alert_symptoms_do_not_reveal_the_root_cause(spec: ScenarioSpec) -> None:
    for symptom in spec.alert.symptoms:
        assert not any(word in symptom.lower() for word in ROOT_CAUSE_WORDS), symptom
        assert not VERSION.search(symptom), symptom


@pytest.mark.parametrize("spec", SPECS, ids=by_id)
def test_scenarios_stay_inside_the_policy_allowlist(spec: ScenarioSpec, settings: Settings) -> None:
    assert spec.namespace in settings.policies.allowed_namespaces
    assert set(spec.services) <= set(settings.policies.allowed_services)
    assert spec.expected["fix"] in settings.policies.remediation_tools


@pytest.mark.parametrize("spec", SPECS, ids=by_id)
def test_only_red_team_scenarios_contain_injection_text(spec: ScenarioSpec) -> None:
    flagged = [line.message for line in spec.log_lines if scan_text(line.message)]
    assert bool(flagged) == spec.expected["redteam"]


@pytest.mark.parametrize("spec", SPECS, ids=by_id)
async def test_reset_reveals_only_the_alert(spec: ScenarioSpec, tmp_path: Path) -> None:
    env = SimulatedEnvironment(
        run_id="run-alert", scenarios_dir=str(SCENARIOS), state_dir=str(tmp_path)
    )
    alert = await env.reset(spec.id, spec.seed)
    assert alert.metadata == {"source": "simulated-alertmanager", "namespace": spec.namespace}
    assert (alert.service, alert.fired_at) == (spec.alert.service, spec.incident_time)
    text = alert.model_dump_json()
    assert spec.family not in text and spec.expected["mechanism"] not in text


@pytest.mark.parametrize("spec", SPECS, ids=by_id)
async def test_the_expected_fix_recovers_the_scenario(
    spec: ScenarioSpec, settings: Settings, tmp_path: Path
) -> None:
    env = await reset(spec, tmp_path)
    oracle = EnvironmentHealthOracle(env, settings.policies.oracle.max_error_rate)
    before = await oracle.probe()
    root = spec.expected["root_cause_service"]
    assert root in before.unhealthy_services

    if spec.expected["fix"] == "execute_rollback":  # the dry run must agree with ground truth
        plan = await env.plan_rollback(root)
        assert plan.target_version == spec.expected["fix_params"]["to_version"]
    verb = FIX_ACTIONS[spec.expected["fix"]]  # remediation tool -> environment action
    action = AuthorizedAction(action=verb, target=root, params=spec.expected["fix_params"])
    assert (await act(env, action)).ok
    assert recovery_result(before, await oracle.probe(), oracle.name).verified


@pytest.mark.parametrize(
    "spec", [s for s in SPECS if s.expected["mechanism"] == "bad_deployment"], ids=by_id
)
async def test_a_restart_never_fixes_a_bad_release(spec: ScenarioSpec, tmp_path: Path) -> None:
    env = await reset(spec, tmp_path)
    restart = AuthorizedAction(action="restart", target=spec.expected["root_cause_service"])
    assert (await act(env, restart)).ok
    assert not (await EnvironmentHealthOracle(env).probe()).healthy


# --- catalog entries --------------------------------------------------------------------


def test_to_info_copies_the_public_catalog_fields() -> None:
    info = to_info(load_scenario(SCENARIOS, "malicious-log"))
    assert (info.id, info.alert_service, info.partition) == ("malicious-log", "cart", "demo")
    assert info.run_overrides == {"planner": "prompt_injected"}
    assert info.expected["redteam"] is True
    assert info.expected["fix_params"] == {"to_version": "v2.0.0"}
    assert "final_state" not in info.expected  # only set when a run should not end RESOLVED
    assert (info.default_seed, info.budget, info.requires_memory_of) == (41, {}, [])

    caution = to_info(load_scenario(SCENARIOS, "memory-caution"))
    assert caution.requires_memory_of == ["bad-deployment"]
    assert to_info(load_scenario(SCENARIOS, "budget-exhausted")).budget == {"max_tool_calls": 3}


def test_list_scenarios_returns_the_catalog_sorted_by_id(tmp_path: Path) -> None:
    env = SimulatedEnvironment(
        run_id="run-list", scenarios_dir=str(SCENARIOS), state_dir=str(tmp_path)
    )
    ids = [info.id for info in env.list_scenarios()]
    assert ids == sorted(ids) and len(ids) == 15


# --- the schema rejects broken files ----------------------------------------------------


BASE = yaml.safe_load((SCENARIOS / "bad-deployment.yaml").read_text(encoding="utf-8"))


def drop_current_checkout_release(data: dict[str, Any]) -> None:
    data["deployments"] = [d for d in data["deployments"] if d["version"] != "v2.4.1"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(colour="blue"), "Extra inputs are not permitted"),
        (lambda d: d["alert"].update(service="billing"), "unknown service 'billing'"),
        (lambda d: d["services"]["payment"].update(dependencies=["frontend"]), "cycle"),
        (drop_current_checkout_release, "latest deployment is v2.4.0"),
        (lambda d: d["faults"][0].update(type="cosmic_ray"), "cosmic_ray"),
        (lambda d: d["expected"].update(mechanism="gremlins"), "mechanism"),
        (lambda d: d.update(budget={"max_toolcalls": 3}), "max_toolcalls"),
        (lambda d: d.update(run_overrides={"planner": "evil"}), "planner"),
    ],
    ids=[
        "unknown-field",
        "unknown-service",
        "dependency-cycle",
        "history-mismatch",
        "unknown-fault-type",
        "unknown-mechanism",
        "unknown-budget-field",
        "invalid-run-override",
    ],
)
def test_invalid_scenarios_are_rejected(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    data = copy.deepcopy(BASE)
    mutate(data)
    (tmp_path / "bad-deployment.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_scenario(tmp_path, "bad-deployment")


def test_id_must_match_the_file_name(tmp_path: Path) -> None:
    (tmp_path / "renamed.yaml").write_text(yaml.safe_dump(BASE), encoding="utf-8")
    with pytest.raises(ValueError, match="must equal the file name"):
        load_scenario(tmp_path, "renamed")


def test_unknown_or_unsafe_scenario_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown scenario"):
        load_scenario(SCENARIOS, "no-such-scenario")
    with pytest.raises(ValueError, match="invalid scenario id"):
        load_scenario(SCENARIOS, "../../configs/policies")


def test_a_scenario_cannot_use_run_overrides_to_switch_off_safety(tmp_path: Path) -> None:
    """Scenario files are data: run_overrides may only set planner and strategy."""
    shipped = Path(SCENARIOS) / "bad-deployment.yaml"
    text = shipped.read_text(encoding="utf-8").replace(
        "run_overrides: {}", "run_overrides: {safety_enabled: false}"
    )
    assert "safety_enabled: false" in text
    (tmp_path / "bad-deployment.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="run_overrides may only set"):
        load_scenario(tmp_path, "bad-deployment")
