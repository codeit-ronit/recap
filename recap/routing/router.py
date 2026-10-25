"""Rule-based adaptive router: one investigator, or two specialists in parallel?

Where it sits:

    RoutingSignals + RunConfig --> [RulesRouter.decide] --> StrategyDecision --> Orchestrator

Parallel investigation costs more (two agents, more tool calls), so the router spends that
extra compute only when the signals say the incident is hard. This is a simple, inspectable
form of adaptive inference-time compute. The rules are checked in order and the FIRST match
wins; its rule ID and a plain-English reason are traced and shown in the dashboard.

    RT0-forced              the run configuration forces "single" or "parallel"
    RT1-budget-floor        too little budget left for two agents             -> single
    RT2-cross-service       two or more services affected                     -> parallel
    RT3-disagreement        last round left a conflict no evidence rule settled -> parallel
    RT3-escalation          last round inconclusive, or a fix already failed  -> parallel
    RT4-memory-conflict     similar past incidents disagree                   -> parallel
    RT5-ambiguous-symptoms  symptoms span three or more categories            -> parallel
    RT6-familiar            a verified fix exists for a very similar incident -> single
    RT7-default             a clear single-service incident                   -> single

`LearnedRouter` only marks where a trained policy would plug in. Nothing is trained in V1.
"""

from __future__ import annotations

from collections.abc import Mapping

from recap.agents.roles import SPECIALISTS
from recap.core.config import AgentSpec, RoutingSettings
from recap.core.models import AgentRole, RoutingSignals, RunConfig, StrategyDecision, StrategyMode

SINGLE, PARALLEL = StrategyMode.SINGLE, StrategyMode.PARALLEL
Choice = tuple[StrategyMode, str, str]  # (mode, rule_id, reason)


class RulesRouter:
    """Deterministic routing rules, first match wins (implements RouterPolicy)."""

    name = "rules-router-v1"

    def __init__(self, routing: RoutingSettings, specs: Mapping[AgentRole, AgentSpec]) -> None:
        self.routing = routing
        self.specs = dict(specs)

    def decide(
        self, signals: RoutingSignals, config: RunConfig, round_number: int
    ) -> StrategyDecision:
        """Choose single or parallel for one round. Same inputs always give the same decision."""
        mode, rule_id, reason = self._first_match(signals, config)
        agents = list(SPECIALISTS) if mode == PARALLEL else [AgentRole.INVESTIGATOR]
        return StrategyDecision(
            mode=mode,
            reason=reason,
            rule_id=rule_id,
            selected_agents=agents,
            provider="",  # the controller fills in the provider that will drive the agents
            budget=self._step_limits(agents, signals.remaining_steps),
            signals=signals,
            round=round_number,
        )

    def _first_match(self, s: RoutingSignals, config: RunConfig) -> Choice:
        """Walk the rules in order and return the first one that applies."""
        if config.strategy != "adaptive":
            return _forced(config.strategy, s)
        floor = self.routing.parallel_min_tool_calls
        if s.remaining_tool_calls < floor or s.max_parallel_agents < 2:
            return (
                SINGLE,
                "RT1-budget-floor",
                "not enough budget left for parallel specialists "
                f"({s.remaining_tool_calls} tool calls left, parallel needs {floor}; "
                f"at most {s.max_parallel_agents} agent(s) at once)",
            )
        if s.services_affected >= 2:
            return (
                PARALLEL,
                "RT2-cross-service",
                f"{s.services_affected} services are affected: look at telemetry and "
                "infrastructure side by side",
            )
        if s.disagreement:
            return (
                PARALLEL,
                "RT3-disagreement",
                "the last round left claims that conflict (level root causes, or different "
                "bad releases) and no evidence rule settles them: ask both specialists again, "
                "with the verifier's open questions",
            )
        if s.escalation or s.prior_failed_attempts > 0:
            return (
                PARALLEL,
                "RT3-escalation",
                "previous round was inconclusive or an earlier fix failed: spend more compute",
            )
        if s.memory_conflict:
            return (
                PARALLEL,
                "RT4-memory-conflict",
                "similar past incidents disagree on the cause or the outcome: "
                "get two independent views",
            )
        if len(s.symptom_categories) >= 3:
            kinds = ", ".join(s.symptom_categories)
            return (
                PARALLEL,
                "RT5-ambiguous-symptoms",
                f"symptoms span {len(s.symptom_categories)} categories ({kinds}): the cause is "
                "ambiguous",
            )
        if s.familiar_success:
            return (
                SINGLE,
                "RT6-familiar",
                "verified similar incident in memory; one investigator confirms it",
            )
        return SINGLE, "RT7-default", "clear single-service incident"

    def _step_limits(self, agents: list[AgentRole], remaining_steps: int) -> dict[str, int]:
        """Per-agent step limit: the role's max_steps, capped by its share of the steps left.

        The share never drops below 1, so an agent with no steps left still tries once and
        the budget tracker (not a silent no-op) ends the run as BUDGET_EXHAUSTED.
        """
        share = max(1, remaining_steps // len(agents))
        return {role.value: min(self.specs[role].max_steps, share) for role in agents}


def _forced(strategy: str, s: RoutingSignals) -> Choice:
    """RT0: the run configuration overrides the rules, but parallel still needs room for two."""
    if strategy == "single":
        return SINGLE, "RT0-forced", "single investigator forced by run configuration"
    if s.max_parallel_agents >= 2 and s.remaining_tool_calls >= 2:
        return PARALLEL, "RT0-forced", "parallel specialists forced by run configuration"
    return (
        SINGLE,
        "RT0-forced",
        "parallel was forced by run configuration, but the budget leaves room for only one agent",
    )


class LearnedRouter:
    """Interface stub ONLY: where a trained routing policy would plug in. Nothing is trained.

    V2 plan: a contextual bandit over RoutingSignals, rewarded for verified recovery and
    penalised for tool calls and time. It has the same `decide` signature as RulesRouter, so
    swapping it in would not change the controller. In V1, calling it always raises.
    """

    name = "learned-router (not trained)"

    def decide(
        self, signals: RoutingSignals, config: RunConfig, round_number: int
    ) -> StrategyDecision:
        """Always raises NotImplementedError: V1 has no trained router."""
        raise NotImplementedError(
            "V2 plan: contextual bandit over routing signals; nothing is trained in V1"
        )
