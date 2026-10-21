"""Tests for the rule-based router (each rule in its own scenario), roles and the learned stub."""

from __future__ import annotations

from typing import Any

import pytest

from recap.agents.roles import INVESTIGATION_ROLES, SPECIALISTS, agent_specs
from recap.core.config import Settings
from recap.core.errors import ConfigError
from recap.core.interfaces import RouterPolicy
from recap.core.models import AgentRole, RoutingSignals, RunConfig, StrategyMode
from recap.routing.router import LearnedRouter, RulesRouter

SINGLE, PARALLEL = StrategyMode.SINGLE, StrategyMode.PARALLEL
ADAPTIVE = RunConfig()


def signals(**overrides: Any) -> RoutingSignals:
    """A clear single-service incident with plenty of budget, plus `overrides`."""
    base: dict[str, Any] = {
        "services_affected": 1,
        "symptom_categories": ["errors", "latency"],
        "remaining_tool_calls": 30,
        "remaining_steps": 30,
        "max_parallel_agents": 2,
    }
    return RoutingSignals(**{**base, **overrides})


@pytest.fixture
def router(settings: Settings) -> RulesRouter:
    return RulesRouter(settings.budget.routing, agent_specs(settings))


@pytest.mark.parametrize(
    ("overrides", "mode", "rule_id"),
    [
        ({"remaining_tool_calls": 9}, SINGLE, "RT1-budget-floor"),
        ({"max_parallel_agents": 1}, SINGLE, "RT1-budget-floor"),
        ({"services_affected": 2}, PARALLEL, "RT2-cross-service"),
        ({"escalation": True, "disagreement": True}, PARALLEL, "RT3-disagreement"),
        ({"escalation": True}, PARALLEL, "RT3-escalation"),
        ({"prior_failed_attempts": 1}, PARALLEL, "RT3-escalation"),
        ({"memory_conflict": True}, PARALLEL, "RT4-memory-conflict"),
        (
            {"symptom_categories": ["availability", "errors", "latency"]},
            PARALLEL,
            "RT5-ambiguous-symptoms",
        ),
        ({"familiar_success": True}, SINGLE, "RT6-familiar"),
        ({}, SINGLE, "RT7-default"),
    ],
)
def test_each_rule_fires_in_its_own_scenario(
    router: RulesRouter, overrides: dict[str, Any], mode: StrategyMode, rule_id: str
) -> None:
    decision = router.decide(signals(**overrides), ADAPTIVE, round_number=1)
    assert (decision.mode, decision.rule_id) == (mode, rule_id)
    assert decision.reason  # every decision explains itself in plain English


def test_budget_floor_overrides_cross_service(router: RulesRouter) -> None:
    decision = router.decide(signals(services_affected=3, remaining_tool_calls=4), ADAPTIVE, 1)
    assert (decision.mode, decision.rule_id) == (SINGLE, "RT1-budget-floor")
    assert decision.reason.startswith("not enough budget left for parallel specialists")


def test_the_first_matching_rule_wins(router: RulesRouter) -> None:
    hard_but_familiar = signals(services_affected=2, familiar_success=True)
    assert router.decide(hard_but_familiar, ADAPTIVE, 1).rule_id == "RT2-cross-service"
    retry_of_familiar = signals(escalation=True, familiar_success=True)
    assert router.decide(retry_of_familiar, ADAPTIVE, 2).rule_id == "RT3-escalation"


@pytest.mark.parametrize(
    ("strategy", "overrides", "mode"),
    [
        ("single", {"services_affected": 3}, SINGLE),
        ("parallel", {}, PARALLEL),
        ("parallel", {"remaining_tool_calls": 4}, PARALLEL),  # below RT1's floor is fine
        ("parallel", {"max_parallel_agents": 1}, SINGLE),  # but two agents must fit
        ("parallel", {"remaining_tool_calls": 1}, SINGLE),
    ],
)
def test_forced_strategies(
    router: RulesRouter, strategy: str, overrides: dict[str, Any], mode: StrategyMode
) -> None:
    forced = RunConfig.model_validate({"strategy": strategy})
    decision = router.decide(signals(**overrides), forced, 1)
    assert (decision.mode, decision.rule_id) == (mode, "RT0-forced")
    assert "forced by run configuration" in decision.reason


def test_single_mode_runs_the_investigator_within_the_remaining_steps(
    router: RulesRouter,
) -> None:
    decision = router.decide(signals(), ADAPTIVE, round_number=2)
    assert decision.selected_agents == [AgentRole.INVESTIGATOR]
    assert decision.budget == {"investigator": 10}  # configs/agents.yaml max_steps
    assert decision.provider == "" and decision.round == 2
    assert decision.signals == signals()
    assert router.decide(signals(remaining_steps=4), ADAPTIVE, 1).budget == {"investigator": 4}


@pytest.mark.parametrize(("remaining", "each"), [(30, 8), (6, 3), (1, 1)])
def test_parallel_mode_splits_the_remaining_steps(
    router: RulesRouter, remaining: int, each: int
) -> None:
    decision = router.decide(signals(services_affected=2, remaining_steps=remaining), ADAPTIVE, 1)
    assert decision.selected_agents == list(SPECIALISTS)
    assert decision.budget == {
        "telemetry_investigator": each,
        "infrastructure_investigator": each,
    }


def test_an_agent_always_gets_at_least_one_step(router: RulesRouter) -> None:
    # With nothing left the agent still tries once, so the budget tracker (not a silent
    # no-op) is what stops the run as BUDGET_EXHAUSTED.
    assert router.decide(signals(remaining_steps=0), ADAPTIVE, 3).budget == {"investigator": 1}


def test_routing_is_deterministic(router: RulesRouter) -> None:
    inputs = signals(memory_conflict=True, remaining_steps=13)
    assert router.decide(inputs, ADAPTIVE, 1) == router.decide(inputs, ADAPTIVE, 1)


def test_both_routers_fit_the_router_interface(router: RulesRouter) -> None:
    assert isinstance(router, RouterPolicy) and router.name == "rules-router-v1"
    assert isinstance(LearnedRouter(), RouterPolicy)


def test_learned_router_is_an_untrained_stub() -> None:
    learned = LearnedRouter()
    assert "not trained" in learned.name
    with pytest.raises(NotImplementedError, match="nothing is trained in V1"):
        learned.decide(signals(), ADAPTIVE, 1)


def test_agent_specs_lists_the_three_investigation_roles(settings: Settings) -> None:
    specs = agent_specs(settings)
    assert tuple(specs) == INVESTIGATION_ROLES
    assert specs[AgentRole.TELEMETRY].tools  # read-only tools from configs/agents.yaml


def test_agent_specs_refuses_a_missing_role(settings: Settings) -> None:
    agents = {role: spec for role, spec in settings.agents.items() if role != AgentRole.TELEMETRY}
    with pytest.raises(ConfigError, match="telemetry_investigator"):
        agent_specs(settings.model_copy(update={"agents": agents}))


def test_an_open_disagreement_on_a_cross_service_incident_still_routes_by_rt2(
    router: RulesRouter,
) -> None:
    """Both rules choose the two specialists; the round's trace carries the open questions."""
    decision = router.decide(signals(services_affected=2, disagreement=True), ADAPTIVE, 2)
    assert (decision.mode, decision.rule_id) == (PARALLEL, "RT2-cross-service")


def test_a_disagreement_never_beats_the_budget_floor(router: RulesRouter) -> None:
    decision = router.decide(signals(remaining_tool_calls=9, disagreement=True), ADAPTIVE, 2)
    assert (decision.mode, decision.rule_id) == (SINGLE, "RT1-budget-floor")
