"""Tests for the central safety policy (rules R0-R12 and R14).

Every decision must come from trusted inputs only: the registry's risk tier, validated
arguments, the caller, the environment and the stored approval.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from recap.core.config import ArgRule, Settings, ToolPolicy
from recap.core.models import (
    AgentRole,
    Approval,
    EnvironmentInfo,
    MitigationProposal,
    PolicyRequest,
    ToolRisk,
    Verdict,
)
from recap.safety import policy as rules
from recap.safety.policy import PolicyEngine
from recap.tools.registry import ToolRegistry

NOW = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)
SIM = EnvironmentInfo(kind="simulated", name="test simulator", namespace="shop", is_simulated=True)
REAL = SIM.model_copy(update={"kind": "kubernetes", "is_simulated": False})


@pytest.fixture
def registry(settings: Settings) -> ToolRegistry:
    return ToolRegistry(settings.policies)


@pytest.fixture
def engine(settings: Settings, registry: ToolRegistry) -> PolicyEngine:
    return PolicyEngine(settings.policies, registry)


def make_proposal(
    action: str = "execute_rollback",
    target: str = "checkout",
    params: dict[str, Any] | None = None,
    **extra: Any,
) -> MitigationProposal:
    fields: dict[str, Any] = {
        "proposal_id": "prop-1",
        "incident_id": "inc-1",
        "action": action,
        "target": target,
        "params": {"to_version": "v2.4.0"} if params is None else params,
        "rationale": "bad release",
        **extra,
    }
    return MitigationProposal(**fields)


def make_approval(proposal: MitigationProposal, **overrides: Any) -> Approval:
    fields: dict[str, Any] = {
        "approval_id": "apr-1",
        "incident_id": proposal.incident_id,
        "proposal_id": proposal.proposal_id,
        "proposal_digest": proposal.digest(),
        "decision": "approved",
        "actor": "alice",
        "decided_at": NOW,
        "expires_at": NOW + timedelta(minutes=15),
        **overrides,
    }
    return Approval(**fields)


def mitigation(
    registry: ToolRegistry,
    proposal: MitigationProposal,
    approval: Approval | None = None,
    **overrides: Any,
) -> PolicyRequest:
    fields: dict[str, Any] = {
        "tool_name": proposal.action,
        "args": registry.mitigation_args(proposal),
        "caller": AgentRole.CONTROLLER,
        "incident_id": "inc-1",
        "environment": SIM,
        "proposal": proposal,
        "approval": approval,
        "now": NOW,
        **overrides,
    }
    return PolicyRequest(**fields)


def read(tool: str = "get_service_health", args: dict[str, Any] | None = None, **overrides: Any):
    fields: dict[str, Any] = {
        "tool_name": tool,
        "args": {"service": "checkout"} if args is None else args,
        "caller": AgentRole.TELEMETRY,
        "incident_id": "inc-1",
        "environment": SIM,
        "now": NOW,
        **overrides,
    }
    return PolicyRequest(**fields)


# --- reads -------------------------------------------------------------------------------


def test_read_tools_are_allowed_automatically(engine: PolicyEngine) -> None:
    decision = engine.evaluate(read())
    assert decision.verdict == Verdict.ALLOW
    assert decision.rule_ids == [
        rules.R1_TOOL_NAME_FORMAT,
        rules.R2_KNOWN_TOOL,
        rules.R3_SAFETY_SCOPE,
        rules.R4_ARGS_VALID,
        rules.R5_NAMESPACE_ALLOWLIST,
        rules.R6_SERVICE_ALLOWLIST,
        rules.R7_WORKER_READ_ONLY,
        rules.R8_READ_ALLOW,
    ]
    assert decision.risk == ToolRisk.READ
    assert decision.caller == AgentRole.TELEMETRY
    assert decision.policy_version == engine.version == "policy-v1"
    assert decision.decided_at == NOW


def test_service_rule_is_recorded_only_when_a_service_argument_exists(
    engine: PolicyEngine,
) -> None:
    decision = engine.evaluate(read("list_services", {}))
    assert decision.verdict == Verdict.ALLOW
    assert rules.R6_SERVICE_ALLOWLIST not in decision.rule_ids


def test_invalid_arguments_are_denied(engine: PolicyEngine) -> None:
    decision = engine.evaluate(read("get_service_logs", {"service": "checkout", "limit": True}))
    assert decision.verdict == Verdict.DENY
    assert decision.rule_ids[-1] == rules.R4_ARGS_VALID
    assert "limit must be an integer" in decision.reasons[0]


def test_service_outside_the_allowlist_is_denied(engine: PolicyEngine) -> None:
    decision = engine.evaluate(read(args={"service": "billing"}))
    assert decision.verdict == Verdict.DENY
    assert "not an allowlisted service" in decision.reasons[0]


def test_environment_namespace_must_be_allowlisted(engine: PolicyEngine) -> None:
    other = SIM.model_copy(update={"namespace": "kube-system"})
    decision = engine.evaluate(read(environment=other))
    assert decision.verdict == Verdict.DENY
    assert decision.rule_ids[-1] == rules.R5_NAMESPACE_ALLOWLIST
    assert decision.reasons == ["namespace 'kube-system' is not in the allowlist"]


# --- tool identity -----------------------------------------------------------------------


def test_unknown_tools_are_denied(engine: PolicyEngine) -> None:
    decision = engine.evaluate(read("read_only_cleanup", {}))
    assert decision.verdict == Verdict.DENY
    assert decision.rule_ids == [rules.R1_TOOL_NAME_FORMAT, rules.R2_KNOWN_TOOL]
    assert "not trusted" in decision.reasons[0]
    assert decision.risk is None


@pytest.mark.parametrize(
    "name",
    [
        "Execute_Rollback",  # case trick
        "execute_rollback ",  # trailing whitespace
        "exеcute_rollback",  # Cyrillic "е" look-alike
        "get_service_health;delete_namespace",  # injected separator
        "execute_rollback\n",
        "",
    ],
)
def test_name_tricks_are_denied_by_the_format_rule(engine: PolicyEngine, name: str) -> None:
    decision = engine.evaluate(read(name, {}, caller=AgentRole.CONTROLLER))
    assert decision.verdict == Verdict.DENY
    assert decision.rule_ids == [rules.R1_TOOL_NAME_FORMAT]


# --- writes ------------------------------------------------------------------------------


def test_workers_may_not_write_even_with_an_approval(
    engine: PolicyEngine, registry: ToolRegistry
) -> None:
    proposal = make_proposal()
    request = mitigation(registry, proposal, make_approval(proposal), caller=AgentRole.TELEMETRY)
    decision = engine.evaluate(request)
    assert decision.verdict == Verdict.DENY
    assert decision.rule_ids[-1] == rules.R7_WORKER_READ_ONLY


def test_reversible_write_without_approval_requires_one(
    engine: PolicyEngine, registry: ToolRegistry
) -> None:
    decision = engine.evaluate(mitigation(registry, make_proposal()))
    assert decision.verdict == Verdict.APPROVAL_REQUIRED
    assert decision.rule_ids[-1] == rules.R11_REVERSIBLE_APPROVAL
    assert decision.risk == ToolRisk.REVERSIBLE_WRITE


def test_reversible_write_without_a_proposal_is_denied(engine: PolicyEngine) -> None:
    args = {"service": "checkout", "to_version": "v2.4.0"}
    decision = engine.evaluate(read("execute_rollback", args, caller=AgentRole.CONTROLLER))
    assert decision.verdict == Verdict.DENY
    assert decision.reasons == ["mitigations must come from a proposal"]


def test_destructive_is_denied_even_for_the_controller_with_an_approval(
    engine: PolicyEngine, registry: ToolRegistry
) -> None:
    proposal = make_proposal("delete_namespace", "shop", {})
    decision = engine.evaluate(mitigation(registry, proposal, make_approval(proposal)))
    assert decision.verdict == Verdict.DENY
    assert decision.rule_ids[-1] == rules.R9_DESTRUCTIVE_DENY
    assert rules.R12_APPROVAL_BINDING not in decision.rule_ids
    assert "never automated" in decision.reasons[0]


def test_declared_risk_in_the_proposal_is_ignored(
    engine: PolicyEngine, registry: ToolRegistry
) -> None:
    proposal = make_proposal("delete_namespace", "shop", {}, risk_level=ToolRisk.READ)
    decision = engine.evaluate(mitigation(registry, proposal))
    assert decision.verdict == Verdict.DENY
    assert decision.risk == ToolRisk.DESTRUCTIVE


def test_high_risk_tools_are_denied_in_v1(settings: Settings) -> None:
    tools = {
        **settings.policies.tools,
        "scale_to_zero": ToolPolicy(
            risk=ToolRisk.HIGH_RISK, args={"service": ArgRule(type="service")}
        ),
    }
    policies = settings.policies.model_copy(update={"tools": tools})
    registry = ToolRegistry(policies)
    proposal = make_proposal("scale_to_zero", "checkout", {})
    decision = PolicyEngine(policies, registry).evaluate(
        mitigation(registry, proposal, make_approval(proposal))
    )
    assert decision.verdict == Verdict.DENY
    assert decision.rule_ids[-1] == rules.R10_HIGH_RISK_DENY


# --- remediation catalog (R14) -----------------------------------------------------------


def test_a_read_tool_cannot_be_proposed_as_a_mitigation(
    engine: PolicyEngine, registry: ToolRegistry
) -> None:
    # Plain reads are allowed automatically (R8); as a "mitigation" one would skip approval.
    proposal = make_proposal("get_service_logs", "checkout", {})
    for approval in (None, make_approval(proposal)):
        decision = engine.evaluate(mitigation(registry, proposal, approval))
        assert decision.verdict == Verdict.DENY
        assert decision.rule_ids[-1] == rules.R14_REMEDIATION_CATALOG
        assert decision.reasons == [
            "mitigations must use a tool from the trusted remediation catalog"
        ]


def test_a_write_outside_the_remediation_catalog_is_denied(settings: Settings) -> None:
    policies = settings.policies.model_copy(update={"remediation_tools": ["execute_rollback"]})
    registry = ToolRegistry(policies)
    proposal = make_proposal("restart_service", "checkout", {})
    decision = PolicyEngine(policies, registry).evaluate(mitigation(registry, proposal))
    assert decision.verdict == Verdict.DENY
    assert decision.rule_ids[-1] == rules.R14_REMEDIATION_CATALOG


# --- approval binding (R12) --------------------------------------------------------------


def test_a_matching_approval_allows_the_write(engine: PolicyEngine, registry: ToolRegistry) -> None:
    proposal = make_proposal()
    decision = engine.evaluate(mitigation(registry, proposal, make_approval(proposal)))
    assert decision.verdict == Verdict.ALLOW
    assert decision.rule_ids[-2:] == [rules.R11_REVERSIBLE_APPROVAL, rules.R12_APPROVAL_BINDING]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"decision": "rejected"}, "approval was rejected by alice"),
        ({"incident_id": "inc-other"}, "approval belongs to a different incident"),
        ({"proposal_id": "prop-other"}, "approval is for a different proposal"),
        ({"proposal_digest": "0" * 64}, "approval digest does not match the proposal"),
        ({"expires_at": NOW}, "approval expired"),
        ({"consumed_at": NOW - timedelta(seconds=1)}, "approval was already used"),
    ],
)
def test_each_binding_failure_is_denied_with_a_specific_reason(
    engine: PolicyEngine, registry: ToolRegistry, overrides: dict[str, Any], reason: str
) -> None:
    proposal = make_proposal()
    decision = engine.evaluate(mitigation(registry, proposal, make_approval(proposal, **overrides)))
    assert decision.verdict == Verdict.DENY
    assert decision.rule_ids[-1] == rules.R12_APPROVAL_BINDING
    assert len(decision.reasons) == 1 and decision.reasons[0].startswith(reason)


def test_approval_for_a_different_proposal_is_denied(
    engine: PolicyEngine, registry: ToolRegistry
) -> None:
    approved = make_proposal()
    other = make_proposal(target="payment", params={"to_version": "v1.0.0"}, proposal_id="prop-2")
    decision = engine.evaluate(mitigation(registry, other, make_approval(approved)))
    assert decision.verdict == Verdict.DENY
    assert "approval is for a different proposal" in decision.reasons
    assert any("digest" in reason for reason in decision.reasons)


def test_changing_params_after_approval_breaks_the_digest(
    engine: PolicyEngine, registry: ToolRegistry
) -> None:
    proposal = make_proposal()
    approval = make_approval(proposal)
    edited = proposal.model_copy(update={"params": {"to_version": "v1.0.0"}})  # same proposal_id
    decision = engine.evaluate(mitigation(registry, edited, approval))
    assert decision.verdict == Verdict.DENY
    assert decision.reasons == [
        "approval digest does not match the proposal (it changed after approval)"
    ]


def test_request_arguments_must_match_the_approved_proposal(
    engine: PolicyEngine, registry: ToolRegistry
) -> None:
    proposal = make_proposal()
    args = {"service": "payment", "to_version": "v2.4.0"}  # same tool, different target
    decision = engine.evaluate(mitigation(registry, proposal, make_approval(proposal), args=args))
    assert decision.verdict == Verdict.DENY
    assert decision.reasons == ["arguments differ from the proposal"]


@pytest.mark.parametrize("named", ["payment", "checkout"])  # a decoy, or the target twice
def test_params_may_not_set_the_target_argument(
    engine: PolicyEngine, registry: ToolRegistry, named: str
) -> None:
    # The approver reads the params, but the target is what runs: they must never differ.
    proposal = make_proposal("restart_service", "checkout", {"service": named})
    reason = "proposal params may not set the target argument; use the proposal target"
    asked = engine.evaluate(mitigation(registry, proposal))
    assert asked.verdict == Verdict.DENY and asked.reasons == [reason]
    assert asked.rule_ids[-1] == rules.R11_REVERSIBLE_APPROVAL
    approved = engine.evaluate(mitigation(registry, proposal, make_approval(proposal)))
    assert approved.verdict == Verdict.DENY and approved.reasons == [reason]
    assert approved.rule_ids[-1] == rules.R12_APPROVAL_BINDING


def test_proposal_from_another_incident_is_denied_before_asking_a_human(
    engine: PolicyEngine, registry: ToolRegistry
) -> None:
    proposal = make_proposal(incident_id="inc-other")
    decision = engine.evaluate(mitigation(registry, proposal))
    assert decision.verdict == Verdict.DENY
    assert decision.reasons == ["proposal belongs to a different incident"]


# --- safety switch (R3 / R0) -------------------------------------------------------------


def test_safety_disabled_in_simulation_allows_and_records_the_real_verdict(
    engine: PolicyEngine, registry: ToolRegistry
) -> None:
    proposal = make_proposal("delete_namespace", "shop", {})
    decision = engine.evaluate(mitigation(registry, proposal, safety_enabled=False))
    assert decision.verdict == Verdict.ALLOW
    assert decision.rule_ids[-1] == rules.R0_SAFETY_DISABLED
    assert rules.R9_DESTRUCTIVE_DENY in decision.rule_ids
    assert "would have been DENY" in decision.reasons[0]
    assert "never automated" in decision.reasons[0]


def test_safety_can_only_be_disabled_in_a_simulation(engine: PolicyEngine) -> None:
    decision = engine.evaluate(read(environment=REAL, safety_enabled=False))
    assert decision.verdict == Verdict.DENY
    assert decision.reasons == ["safety can only be disabled in a simulated environment"]


def test_safety_disabled_still_rejects_unknown_and_malformed_tools(engine: PolicyEngine) -> None:
    unknown = engine.evaluate(read("read_only_cleanup", {}, safety_enabled=False))
    assert unknown.verdict == Verdict.DENY and unknown.rule_ids[-1] == rules.R2_KNOWN_TOOL
    malformed = engine.evaluate(read("Delete_Namespace", {}, safety_enabled=False))
    assert malformed.verdict == Verdict.DENY and malformed.rule_ids[-1] == rules.R1_TOOL_NAME_FORMAT


def test_decisions_are_deterministic(engine: PolicyEngine, registry: ToolRegistry) -> None:
    request = mitigation(registry, make_proposal())
    assert engine.evaluate(request) == engine.evaluate(request)
